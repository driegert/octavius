# Extending Octavius

Read this before adding a feature, tool, route, page or external dependency. The main question is where new behaviour lives.

## Placement

- Voice/session behaviour: `conversation.py`, `agent.py`, `websocket_session.py` (or the matching `ws_*.py` mixin for its message family).
- HTTP routes: the relevant `routes/*.py`, with orchestration pushed down into subsystem modules. `main.py` is composition only; startup/shutdown is `app_lifespan.py`; headers are `ui_security.py`.
- Reader: `reader_ingest_*`, `reader_store.py`, `reader_text.py`, `reader_playback.py`.
- Inbox/history queries and persistence: `history_store.py` / `history_enrichment.py`, not route handlers.
- Local tools: schema in `local_tool_specs.py`, behaviour in the right `local_tool_*` module, wired through `tools.py`. Test both the handler and dispatch. If pi should see it, also check `pi_tools_server.py`'s list.
- Outbound services: behind `service_clients.py` or a close wrapper, with timeouts, retries, fallback and health hooks at the client boundary.
- UI: browser logic in the page's `static/*-app.js`; obey the CSP rules in [frontend.md](frontend.md).

## Considerations

- Background tasks: decide explicitly what happens on restart and whether retry/requeue is needed (reader jobs and docproc ids do not survive restarts).
- If a feature depends on MCP or LLM availability, design the degraded path and the user-visible failure message; make `/health` or logs show it.
- A changed persisted shape or workflow updates docs and tests in the same change.
- Prefer extending existing seams over another thin facade layer; keep SQLite connections short-lived.

## Done means

1. Code in the right boundary; 2. happy path works; 3. at least one failure/degraded-path test where it matters; 4. `/health`, logs or status stay understandable; 5. `AGENTS.md` (or the relevant runbook) updated if stable architecture or workflow changed.
