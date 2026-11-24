# Octavius

Self-hosted voice assistant on Dave's homelab: a FastAPI app on triplestuffed serving a browser voice UI (`/`), a document reader (`/reader`) and a legacy stash inbox (`/inbox`) over one WebSocket protocol, also used by the Android client and the Matrix sidecar. Turns run on a pi agent process (or the native agent loop) against self-hosted LLMs, MCP tool servers, faster-whisper STT and Phonon TTS. Inference, speech and storage are self-hosted on the Tailnet; the exceptions are web search (Parallel.ai, Serper) and whatever pages `read_url` fetches.

This file is the always-loaded core: commands, architecture, code map and the traps that bite any session. Everything else is in `docs/runbooks/` — read the one that matches your task (index at the end). `CLAUDE.md` is a symlink to this file (Codex reads `AGENTS.md`, Claude Code `CLAUDE.md`, same text): edit `AGENTS.md` only.

## Runbook

```bash
uv sync                                    # install deps
systemctl --user start octavius            # normal service
systemctl --user restart octavius
python -m unittest discover -s tests       # tests
scripts/octavius-models show               # what model routing loads, and from where
scripts/octavius-models check              # probe every endpoint: reachable? alias present? authed? (nonzero on failure)
scripts/octavius-models apply              # check, then daemon-reload + restart + confirm /health
```

- FastAPI binds `127.0.0.1:8030`; Caddy exposes `https://octavius.riegert.xyz`.
- Foreground with the service's config (a bare `uv run python main.py` opens the stale repo DB and the startup guard refuses it; the unit sets `OCTAVIUS_DB_PATH`):

  ```bash
  systemctl --user stop octavius   # it holds :8030
  set -a; source ~/.config/octavius/env; source ~/.config/secrets.env; set +a
  OCTAVIUS_DB_PATH=/media/extra_stuff/octavius/octavius_history.db uv run python main.py
  ```

  The same `set -a; source ...; set +a` line is what `scripts/octavius-models check` needs in a shell, so it sees the lilripper key.
- Service config: `~/.config/octavius/env` (non-secret, plain `KEY=value` lines read by `EnvironmentFile=` in the `octavius.service.d/env.conf` drop-in) and `~/.config/octavius/models.json` (LLM routing). The lilripper token lives only in `~/.config/secrets.env`. After editing: `systemctl --user daemon-reload && systemctl --user restart octavius`.

## Validation

1. `python -m unittest discover -s tests` before/after backend changes.
2. `scripts/octavius-models check` before restarting after any routing or endpoint change.
3. After touching routing, WebSocket, reader or inbox flows: start the app; open `/`, `/inbox`, `/reader`; confirm the WebSocket connects, inbox list/load/update works, and reader listing and ingest work.
4. Check `/health`: `alive`/`ready`/`degraded` as expected, `llm_chain` endpoint order and failover state, `mcp.servers` if startup is degraded, `pi_brain` if the pi brain is on.
5. When touching an external-service boundary, verify the endpoint is reachable before assuming an application bug.
6. UI changes: browser-verify under the CSP (see `docs/runbooks/frontend.md`).

## Architecture

```text
Browser / Android / Matrix sidecar ──/ws──> FastAPI (main.py)
                                              │  websocket_session.py + ws_* mixins
                                              ├─ STT: faster-whisper (127.0.0.1:8502) + Silero VAD
                                              ├─ turn brain, per turn:
                                              │    pi (live; OCTAVIUS_BRAIN=pi, code default native): `pi --mode rpc` per conversation,
                                              │        tools via pi_harness + Octavius's own tools on 127.0.0.1:8032
                                              │    native (agent.py): streaming loop, ~20 core tools
                                              │        ├─ direct: web search/read, vault, papers, reader, PDF, download, memory, history
                                              │        └─ consult_specialist(domain, task) → subagent.py, inline, scoped tools
                                              │             email (+calendar) · research (openalex) · tasks (vikunja)
                                              ├─ TTS: Phonon (lilripper:8881), sentence-streamed
                                              └─ SQLite history DB (indexed out-of-process by hybrid-corpus)
```

- LLM roles (`main`, `subagent`, `vision`, `reader`, `summary`) are routed by `models.json`; every role falls back to `lilripper:8020`. Image turns use the separate vision chain and stay native.
- The native agent buffers sentences during tool rounds and speaks once tool use is done; rounds cap at 7, tool results truncate at 4000 chars, history trims at 40 messages. Reply style is per channel (voice: short, spoken; text: light markdown). Replies show LaTeX on screen and are spoken as words.
- Each `/ws` connection has its own `Conversation`; a reconnect (`restore_session`) continues the same history record.
- Async delegation (`delegate_task` etc., "Agents at Work" badge) is built but unexposed, reserved for a future `deep_research` domain.

## Code map

Composition and HTTP:
- `main.py` — composition only: builds the app, mounts `/static`, includes routers, registers `/ws`.
- `app_lifespan.py` — startup/shutdown: MCP connect, DB init + `assert_history_serveable`, stale reader jobs, temp-doc purge, subagent dispatcher, pi brain and its tools server.
- `ui_security.py` — CSP and cache headers for `/`, `/inbox`, `/reader` and `/static`.
- `routes/system.py` (`/`, `/health`, `/api/voices`, `/api/chat-models`), `routes/inbox.py`, `routes/conversations.py`, `routes/media.py` (`POST /api/media/upload`), `routes/reader_api.py`, `routes/vault.py`.
- `settings.py` — env-backed settings and `models.json` loading (defaults < file < env); `DEFAULT_MCP_SERVERS`.
- `service_clients.py` — HTTP clients for STT, TTS, the LLM chains, summaries, embeddings; auth headers; failure classification. `stt.py`, `tts.py` (`speechify`, `speakable`), `vad.py` are thin wrappers.
- `db.py`, `media_uploads.py` (upload streaming, spool allowlist), `docproc_client.py`.

WebSocket session:
- `websocket_session.py` — `WebSocketSessionHandler`: dispatch, restore/attach/reset/load, text input, turn running.
- Mixins: `ws_stt.py` (audio, `stt_*`), `ws_reader.py` (reader playback), `ws_doc_chat.py` (Reader chat, agent model menu), `ws_media.py` (`image_input`/`file_input`), `ws_item_chat.py` (inbox item chat), `ws_delegation.py` (inline consults, dormant delegations, proactive speech); `ws_common.py` shared imports. Each logs under its module name.

Agent and tools:
- `conversation.py` (history state), `agent.py` (native LLM loop, tool calling, style/math directives, think stripping).
- `pi_brain.py` (pi RPC processes, failover, warming, confirm relay), `pi_tools_server.py` (loopback MCP server exposing local tools to pi).
- `mcp_manager.py` (MCP lifecycle, routing, truncation, reconnect), `subagent.py` (specialist domains), `subagent_dispatcher.py` (endpoint capacity tickets).
- `tools.py` (local tool dispatch), `local_tool_specs.py` (schemas), `local_tool_*.py` (handlers: `vault`, `reader`, `reader_lookup`, `history`, `memory`, `downloads`, `documents`, `delegations` (dormant), `inbox` (unwired stash helpers)); `local_tool_registry.py` is a compatibility wrapper.
- `vault_files.py` — path-safe, journaling-denied, atomic hash-guarded vault file I/O.

Reader: `document_sources.py`, `reader_ingest_service.py`, `reader_ingest_handlers.py`, `reader_store.py`, `reader_text.py`, `reader_playback.py`.

History: `history.py` (bootstrap, recording), `history_store.py` (queries, inbox CRUD, hybrid search, memory watermarks), `history_enrichment.py` (summaries, tags; embed helpers are raising stubs), `history_sweeper.py` (retired, raises), `memory_client.py`, `stash_to_obsidian.py`, `schema.sql`.

Frontend (`static/`): `octavius.css` + page CSS; `app-common.js`; `/`: `index-app.js`, `index-audio.js`, `index-orb.js`; shared `voice-capture.js`, `math-render.js`; `/reader`: `reader-app.js`, `reader-original.js`, `reader-chat.js`; `/inbox`: `inbox-app.js`; `vendor/` (marked, KaTeX).

Scripts: `scripts/octavius-models`, `scripts/octavius-media-gc` (+ `deploy/` timer units), `scripts/reader-backfill-heading-levels`, `scripts/math-speech-eval`.

Tests: `tests/test_<module>.py` per subsystem; WebSocket behaviour in `tests/test_websocket_session.py` plus `tests/test_ws_{media,reader,item_chat,delegation}.py`; `tests/fake_pi_rpc.py` scripts a pi process for `test_pi_brain.py`.

## Traps that bite any session

- **Nothing loads a `.env` file.** `settings.py` reads `os.environ`; `.env.example` is reference only. Config reaches the service through the systemd drop-in. → `config-and-secrets.md`
- **`secrets.env` uses `export` lines, which systemd's `EnvironmentFile` silently ignores**, so the unit sources it in a subshell. Don't "simplify" that into an `EnvironmentFile`. → `config-and-secrets.md`
- **The live DB is `/media/extra_stuff/octavius/octavius_history.db`** (`OCTAVIUS_DB_PATH` in the unit), not the stale repo copy. → `history-and-search.md`
- **The live brain is pi, and `octavius-models check` does not cover it.** pi's hops are `OCTAVIUS_PI_MODEL` / `OCTAVIUS_PI_FALLBACK_MODELS` (pi_harness models.json), its lilripper key is `PI_AGENT_LR_API_KEY`, and `OCTAVIUS_PI_PATH` must point at nvm's Node 24 (system Node 18 cannot run pi; a stale path fails every pi start). `/health` → `pi_brain` shows its state. → `pi-brain.md`
- **Run `scripts/octavius-models check` before blaming the app** (native chains, vision, reader, summary, subagents). A rotated token (401) and a vanished alias (400) both look like ordinary failovers; `/health`'s `endpoints_rejecting_credentials` and per-endpoint `last_error_kind` say which. → `config-and-secrets.md`
- **Model catalogs and modalities churn.** Re-curl `/v1/models` (llama.cpp routers expose launch args there) before trusting an alias, `--parallel`, or image support. → `model-routing.md`
- **`max_tokens` on a thinking model must budget for reasoning**, or it returns `finish_reason: length` with empty content. gemma4 puts reasoning in `reasoning_content`. → `model-routing.md`
- **`lilripper:8020` is never a primary** (Dave loads models there by hand), and the `OCTAVIUS_LR_API_KEY` header must reach only lilripper (other hosts share port 8010). → `model-routing.md`, `config-and-secrets.md`
- **Treat an unreachable MCP server as fatal at startup.** `connect_all()` catches connect errors and marks the server `connect_failed` in `/health` → `mcp.servers`, yet a web-search URL pointing at an unconfigured port crash-looped the service on 2026-08-26, so loopback MCP units must be up first. The other startup blocker is `assert_history_serveable`: `OCTAVIUS_DB_PATH` must match sites.toml's `[corpora.history] database` and the search sidecars must exist (see the startup log). → `mcp-servers.md`, `history-and-search.md`
- **CSP: no inline `<script>`, `on*=` handlers, `style="..."`, `eval`, or `javascript:` URLs**; third-party JS is vendored; KaTeX via `katex.render`, never `renderToString`. → `frontend.md`
- **Android depends on STT/VAD/`audio_done` semantics**: pure silence never auto-stops, an empty transcription sends no `audio_done`, and every other turn path must end with one. Change them only together with `../octavius-android`. → `voice-and-websocket.md`
- **`docs/ws-media-contract.md` is frozen** and shared with `matrix-agent-sidecar`. → `media-turns.md`
- **Indexing is out-of-process** (`history-index.timer`, every 15 min); the in-app embed helpers raise on purpose. → `history-and-search.md`
- **Vault rules are frozen**: new notes only in `00-zettelkasten/001-Fleeting/`, never touch `03-personal/Journaling/`, never rename or move notes. → `history-and-search.md`

## Coding-agent access (Claude Code, Codex)

- **Claude Code** reads `.mcp.json` (approve on first launch): `conversation-history` and `paper-search` (details in `mcp-servers.md`).
- **Codex** reads `.codex/config.toml`: `paper-search` only. Codex is cloud-connected, so it deliberately does not get `conversation-history`.
- `vikunja-tasks`, `evangeline-email`, `calendar` and `vault-search` are intentionally exposed to neither (personal data / PII).

## Keeping docs current

Update this file only for stable architecture, workflow or a new always-relevant trap; put detail in the matching runbook. Change-oriented status goes in `docs/status.md` (history in `docs/status-archive.md`).

## Runbooks and related docs

| file | read it when |
|---|---|
| `docs/runbooks/config-and-secrets.md` | editing env/systemd config, rotating the lilripper key, triaging `/health` chain errors |
| `docs/runbooks/model-routing.md` | changing `models.json`, endpoints, generation params; slow, empty or failing-over roles; vision hops; lilbuddy sizing |
| `docs/runbooks/pi-brain.md` | pi processes, failover, warming, confirm relay, model menus, pi's tool server |
| `docs/runbooks/mcp-servers.md` | adding/renaming MCP servers or tools; specialist domains and the email prompt rules; web search/read |
| `docs/runbooks/voice-and-websocket.md` | WS message families, STT/VAD, talk modes, barge-in, Android dependency, TTS/Phonon, speech math |
| `docs/runbooks/reader.md` | reader ingest, conversion, playback/progress, append/rename, Original view, Reader chat |
| `docs/runbooks/media-turns.md` | image/PDF turns, uploads, spools and retention, docproc |
| `docs/runbooks/history-and-search.md` | history DB, indexing, embeddings, summaries, history search, vault notes, stash |
| `docs/runbooks/frontend.md` | anything under `static/`, CSP, vendored libs, browser verification |
| `docs/runbooks/extending.md` | adding a feature, tool, route, page or dependency |
| `docs/status.md` | current state and open work (`docs/status-archive.md` for dated history) |
| `docs/ws-media-contract.md` | the frozen `image_input`/`file_input` contract |
| `docs/pi-backend-design.md` | pi brain design and status |
| `docs/pronunciation.md` | TTS pronunciation fixes |
| `docs/HANDOFF-matrix-latency.md`, `docs/HANDOFF-matrix-media.md`, `docs/HANDOFF-docproc-images.md` | Matrix first-turn latency; Matrix media; why converted PDFs have no figure images |
| `README.md`, `octavius-prd.md`, `octavius-android-design.md` | setup summary, product doc, Android design |
