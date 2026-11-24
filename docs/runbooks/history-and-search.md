# History, search, notes and the stash

Read this before touching conversation recording, summaries/tags, history search, embeddings, the vault note tools, or the stash/inbox.

## The database

- **The live database is `/media/extra_stuff/octavius/octavius_history.db`**, set by `OCTAVIUS_DB_PATH` in the service unit (not in `~/.config/octavius/env`). The repo-local `octavius_history.db` is a stale leftover that answers queries with wrong numbers. `systemctl --user show octavius -p Environment` is the authority.
- `schema.sql` holds app-owned tables only; the hybrid-corpus library owns and creates its sidecars in the same file. The same DB is shared with other AI services and exposed through the conversation-history MCP server.
- Request handlers and background jobs use short-lived SQLite connections; live conversation sessions keep a dedicated connection until they end. Don't reintroduce a shared app-wide connection.
- Tests: `create_app`'s lifespan reads `db_init`, `db_path` and `verify_search` from `app.state`; `tests/test_main.py::_isolated_app` patches all of them so a test never touches the real DB.

## Indexing (not the app's job since 2026-09-15)

- Octavius writes `messages`, `conversations.summary` and `saved_items` and stops. `history-index.timer` runs `hybrid-corpus run history` every 15 minutes, fingerprints those tables, and embeds what changed into `history_messages_*`, `history_summaries_*`, `history_saved_items_*`. Recording a turn is pure SQLite — no embedder near the voice path.
- Semantic search is therefore **eventually consistent** (15-minute worst case), except `saved_items`, which `history_store.save_item` indexes inline through the library's `Indexer` (an embed failure is logged and swallowed; the row is keyword-searchable at once, only the vector is owed).
- `app_lifespan` calls `history_store.assert_history_serveable`, which refuses to start if `OCTAVIUS_DB_PATH` and sites.toml's `[corpora.history] database` disagree, or the sidecars are missing or not cosine-metric.
- Retired, raising stubs (a silent no-op would let a stale caller look healthy): `history_sweeper.run_sweeper` / `sweep_once`, and `history_enrichment`'s `store_embedding*` / `spawn_embedding`. `drain_inflight()` still runs at shutdown on an empty set.
- `EMBED_MAX_CHARS` (4000) only applies to the surviving `embed_text`/`embed_text_async` wrappers. The library enforces its own `embed_cap` (8000 in sites.toml) as a **refusal, not a trim**, and chunks long sources (the old path silently indexed a 20k-char message by its first 4000 chars).
- `conversations.indexed` is gone from the schema (the live DB keeps the column harmlessly). `_write_summary` no longer deletes stale vectors; the library re-fingerprints `(service, summary)` and re-embeds rewrites.

## Embedding chain

`OCTAVIUS_EMBEDDING_CHAIN` (bge-m3), default: primary `lilbuddy:8020/v1/embeddings` (standalone llama.cpp, Caddy :8020 → 127.0.0.1:8002, OpenAI schema), fallback `workhorse:11434/api/embeddings` (Ollama schema). `EmbeddingClient` has a **per-endpoint circuit breaker** (2 consecutive failures → skipped 300 s, then one half-open probe) and does not retry `ConnectTimeout` (it subclasses the generic timeout, so a dead host was retried, doubling the cost). With a dead primary capped at two failures, order is purely speed: lilbuddy ~0.11 s vs workhorse ~0.30 s on ~1800 chars (workhorse also pays ~2.7 s cold). `/health` → `embedding_chain` shows `tripped` / `consecutive_failures` / `cooldown_remaining`; it does **not** feed `degraded`, because every search path falls back to keyword matching.

## Summaries, tags and memory

- Generated when a conversation ends (endpoints: [model-routing.md](model-routing.md)). The prompt asks for a one-sentence, action-oriented summary and an `index` flag; the flag only decides the push to the memory service (not persisted, gates no embedding). Tags are generated for all conversations.
- Memory push goes over HTTP via `memory_client.py` to the `agent-memory` service. Watermark helpers (`get_memory_watermark` / `set_memory_watermark` / `messages_after_watermark`) live in `history_store.py` over `conversations.last_extracted_message_id` — Octavius imports nothing from agent-memory (an `import memory` left behind killed the push silently 2026-07-02 → 07-13).
- `end_async` still awaits its remote calls (summary, tags, memory push) on the WS control path, so New Chat / reset / attach can stall behind them.

## Search tools

- `search_conversation_history` wraps `history_store.search_conversations()`: hybrid search over `history_summaries` (vec0 KNN + FTS5/BM25, RRF). An embedding outage degrades to lexical-only. Filtered to `service="octavius"`, excludes the current conversation. Optional `source` (`voice`/`matrix`/`text`) and `since` compose onto the service filter; a filter with no query becomes a recency listing (`history_store.list_conversations`), which also surfaces unsummarised conversations.
- `read_conversation` returns a prior transcript by id (user/assistant only), channel-agnostic (a Matrix thread can pull in a voice conversation). Paged — page 1 = most recent, ~3.5k chars/page, long messages capped — because local tool results bypass the MCP 4000-char truncator. Handlers: `local_tool_history.py`.
- History is resumable from the browser via `load_conversation` (see [voice-and-websocket.md](voice-and-websocket.md)).
- `tools.py` dispatches sync local tools on the event loop, so a slow local tool blocks it.

## Vault (the single note store, since 2026-07-09)

The Obsidian vault (`VAULT_PATH`, default `~/Documents/Personal`) is plain `.md` on triplestuffed.

- Agent tools `save_note`, `read_note`, `edit_note`, `commit_edit` (`local_tool_vault.py` over `vault_files.py`). Search is the `vault_search` MCP tool, which reads a derived index, never the files ([mcp-servers.md](mcp-servers.md)).
- Frozen contract rules in `vault_files.py`: new notes land in `00-zettelkasten/001-Fleeting/` only (filename frozen at creation); `03-personal/Journaling/` is never listed, read or written; paths are vault-relative POSIX with traversal/symlink escapes rejected; writes are atomic (temp + `os.replace`, umask-honouring 0664) and hash-guarded (`base_hash` = sha256 of the file bytes; `commit_edit` 409s on conflict).
- REST: `GET /api/vault/recent`, `GET/POST/PUT /api/vault/note`, `GET /api/vault/search` (`routes/vault.py`; search proxies `call_server_tool("vault-search", "search", ...)`).
- Agents never rename or move notes — Dave files them himself.

## Stash (retired as a notes store; kept for non-note payloads)

`saved_items` in the history DB. `save_to_stash` / `list_stash_items` still exist in `local_tool_inbox.py` but are unwired. `stash_to_obsidian.py` exported existing items one-way (watermark-based, `obsidian-stash-export.timer`). Not being deleted: Dave wants it for payloads that don't belong in Obsidian (planned dictation mode, `docs/status.md`). Still live: `process_pdf` results land in a stash item; `/inbox` and `/api/inbox/*` (list/update/`DELETE /api/inbox/{id}`, semantic search); item chat, with the item's capped content (6000 chars) inlined into the prompt (`ws_item_chat.py`; the `read_item_content` tool was removed). `reader_documents.saved_item_id` is a plain foreign key (no `ON DELETE SET NULL`).
