# MCP servers and specialists

Read this before adding/renaming an MCP server or tool, changing a specialist domain or its prompt, or when a tool search returns nothing or junk.

## Registration and tool naming

Servers are registered in `DEFAULT_MCP_SERVERS` (`settings.py`); `MCPManager` (`mcp_manager.py`) owns connect, routing, reconnect and result truncation (4000 chars). **Treat an unreachable MCP server as fatal at startup.** `connect_all()` wraps each server in `except Exception` and records `connect_failed`, yet Octavius crash-looped on 2026-08-26 for the minutes `web-search` pointed at an unconfigured Caddy port (the failure evidently escapes that handler), so it must not start before its loopback units are up. `/health` → `mcp.servers` shows which backends failed.

Tool naming (since 2026-09-23): `MCPManager` keys every tool by its model-facing name and routes it to (server, upstream name). A server with `tool_prefix` exposes `<prefix>_<tool>` (not doubled if the upstream name already starts with it — pi-mcp-adapter's rule). On a collision the **first** registered server keeps the bare name and the later one becomes `<server key>_<name>` with a warning (before 2026-09-23 the later silently overwrote). Code targeting one server (e.g. `routes/vault.py`) uses `call_server_tool(server, upstream_name, ...)` so it never depends on display names. Upstream renames: mcp-tools `RENAMES-2026-09.md`. MCP tool schemas flow through dynamically (`mcp.get_tools_for_servers()` filters live session tools); what does not auto-update is each domain's system prompt.

## Configured servers (from `settings.py`, 2026-11-24)

| key | transport / URL | exposed to | notes |
|---|---|---|---|
| `evangeline-email` | HTTP `triplestuffed:8251/mcp`, prefix `email` | email specialist | `email_hybrid_search`, `email_keyword_search` (was `search_emails`), `email_semantic_search`, `email_get` / `email_get_many` (were `get_email`/`get_emails`), `email_stats`, `email_extract` (was `extract_from_emails`), … |
| `calendar` | HTTP `127.0.0.1:8208/mcp`, prefix `calendar` | email specialist | mcp-tools `server_calendar.py` (`calendar-mcp.service`), split out of the email server 2026-09-23. Not a main-agent tool: "what's on my calendar" goes through `consult_specialist` |
| `web-search` | HTTP `127.0.0.1:8205/mcp` | main agent | see below |
| `web-reader` | HTTP `127.0.0.1:8207/mcp` | main agent | see below |
| `vault-search` | HTTP `triplestuffed:8254/mcp`, prefix `vault` | main agent | mcp-tools `server_vault.py`: sqlite-vec + FTS5 BM25 over the Obsidian vault, RRF-fused. One upstream tool `search` (was `search_vault`), shown as `vault_search`. `03-personal/Journaling/` excluded server-side. Only vault *search* goes through MCP |
| `paper-search` | HTTP `127.0.0.1:8206/mcp` | main agent | mcp-tools `server_papers.py` (`papers-mcp.service`, no Caddy hop): sqlite-vec + FTS5 over Dave's converted Paperpile library at `/media/extra_stuff/papers/`. `search_papers` + `get_paper`. Conversion/indexing owned by mcp-tools (`papers_convert.py` / `papers_indexer.py`, nightly `papers-sync.timer`) |
| `openalex` | stdio `npx -y openalex-research-mcp@0.4.0` (pinned; bump deliberately) | research specialist | `tool_allowlist` limits it to 14 tools |
| `vikunja-tasks` | HTTP `triplestuffed:8252/mcp` | tasks specialist | project names pass through as Dave says them; the server resolves them live (`VIKUNJA_DEFAULT_PROJECT` = Inbox, id 1) |
| `document-processing` | stdio mcp-tools `server_documents_voice_wrapper.py` (its own venv) | code (`docproc_client.py`) | remote conversion on `lilripper:8251/mcp`; see [media-turns.md](media-turns.md) |

**`web-search`** — mcp-tools' `server_serper.py`, a shared streamable-HTTP service on triplestuffed since 2026-08-26 (`web-search.service`, `:8205`, Caddy `:8255` for other hosts), so every consumer shares one process, one key set and one 15-minute result cache. **Parallel.ai is the primary arm** (neural index, excerpts, 5k free/month); **Google via Serper** answers on a Parallel error or zero hits and owns the `kind="news"`/`"scholar"` verticals; SearXNG only when named. Keys live in `mcp-tools/.env` on triplestuffed — nothing is needed in Octavius. Exposes `web_search`; the description suffix scopes it to general lookups (academic work goes to the research specialist). pi's native tool `pi_harness/extensions/web-search/src/index.ts` is a deliberate mirror — change one, change the other. (Earlier history — stdio subprocess, Serper-first with a SearXNG backstop, the bing-junk inversion — is in `docs/status-archive.md`.)

**`web-reader`** — mcp-tools' `server_reader.py`, since 2026-08-26 a fetch **ladder** on triplestuffed (`web-reader.service`, `:8207`, Caddy `:8257`): site recipes (Reddit threads/listings, X, arXiv, Substack) → plain fetch + trafilatura → Crawl4AI on lilripper in stealth mode → archive. Same instance the pi agents use. Exposes `read_url`, the "read" half of search → read → reason.

## Specialists (`consult_specialist`)

The main agent never sees email/research/task schemas (keeps it at ~20 tools instead of ~55 and avoids tool-schema-heavy payloads that caused LLM 500s). `consult_specialist(domain, task)` runs `subagent.py`: a separate non-streaming LLM loop with only that domain's servers (`SUBAGENT_DOMAINS`: `email` → `evangeline-email` + `calendar`; `research` → `openalex`; `tasks` → `vikunja-tasks`), reusing the main process's warm MCP sessions.

- **Inline/synchronous**: runs to completion inside the main agent's tool round (`WebSocketSessionHandler.run_inline_subagent`, in `ws_delegation.py`) and the answer is spoken in the same turn. A `subagent_dispatcher` ticket is reserved per call so consults respect endpoint capacity; per-step progress goes to the UI `status` line. Routing: [model-routing.md](model-routing.md).
- `_compose_result` appends a `===TOOL DATA===` block (verbatim tool observations) so the main agent lifts exact IDs rather than paraphrased numbers.
- **Async delegation is reserved, not removed**: `delegate_task` / `pull_delegation` / `list_pending_delegations` / `cancel_delegation`, the "Agents at Work" badge, `spawn_delegation` / `_run_and_announce`, `proactive_speak`, and the `delegation_*` WS messages are in `ws_delegation.py` but unexposed (no tool specs/handlers). Reserved for a future `deep_research` domain (headless `pi --mode json -p --no-session`, parsing the final assistant `message_end`); re-enabling is one tool spec + one registry line. Plan: `docs/status.md`.

### Email specialist prompt rules

- Uses `email_hybrid_search` (RRF of semantic + BM25) for anything matching body text.
- **Folder scope is a closed set of five**: `"Inbox"` (default), `"Read Later"`, `"Follow-Up"`, `"Todo"`, or All (`folder=null`). All requires Dave to ask explicitly ("search all my email"). Changed 2026-08-14: passing `folder=null` everywhere searched 107 folders / 28k messages for Inbox questions.
- Age is not scope: "it's an old one" → `date_after="1970-01-01"` and *keep* `folder="Inbox"`.
- **Folder names are exact and case-sensitive, and a wrong name returns zero results, not an error** — `folder="INBOX"` matches nothing (the folder is `Inbox`). Read a zero-result folder search as a suspected typo first.
- Every email tool defaults to All folders and applies no date cutoff (current prompt, `subagent.py`), so the prompt makes `folder` explicit on every call: omitting it silently widens the search past the Inbox default. (Before the 2026-09 renames the tools disagreed, some defaulting to Inbox plus a 6-month lookback.)

## Coding-agent MCP access

- Claude Code (`.mcp.json`): `conversation-history` at `http://127.0.0.1:8203/mcp` (mcp-tools `server_history.py`, `conversation-history.service`; also carries the inbox tools `save_to_inbox`, `search_inbox`, `list_inbox`, `get_inbox_item`, `update_inbox_item`) and `paper-search` at `http://127.0.0.1:8206/mcp` (`papers-mcp.service`; no PII).
- Codex (`.codex/config.toml`): `paper-search` only, because Codex is cloud-connected.
