# Octavius status

Short-to-medium-term state: what is running, what is open, where complexity sits. Durable rules live in `AGENTS.md` and `docs/runbooks/`. Dated history (incidents, fixes, measurements before 2026-11-24) is in `docs/status-archive.md`.

## Current state (2026-11-24)

- **Service healthy** on triplestuffed (`octavius` user unit, `127.0.0.1:8030`, `https://octavius.riegert.xyz`).
- **Routing** is what `~/.config/octavius/models.json` loads (2026-10-31): main and vision on lilbuddy `gemma4-26b-a4b`; subagent, reader and summary on workhorse `gamma` (Gemma4-12B); `lilripper:8020` `xray` (Inkling2-mini) as every role's fallback; workhorse as the main chain's third hop. Table and reasons: `docs/runbooks/model-routing.md`.
- **pi brain live** since 2026-10-31 (`OCTAVIUS_BRAIN=pi`), primary `lrv-p01/xray` since 2026-11-26 (then the 26B, then gamma); native brain still handles image, vision-thread, item-chat and file turns.
- **Refactor landed 2026-11-24**: `main.py` is composition only (`app_lifespan.py` startup/shutdown, `ui_security.py` CSP + cache headers, `routes/system.py` for `/`, `/health`, `/api/voices`, `/api/chat-models`); `websocket_session.py` is the core handler with mixins `ws_stt.py`, `ws_reader.py`, `ws_doc_chat.py`, `ws_media.py`, `ws_item_chat.py`, `ws_delegation.py` and `ws_common.py`; WS tests split into `tests/test_ws_*.py`; `static/index-app.js` uses `static/voice-capture.js` instead of its own copy. Log records from moved code carry the new logger names (`ws_media`, `ws_stt`, …).
- **Docs restructured 2026-11-24**: `AGENTS.md` trimmed to the always-loaded core; detail in `docs/runbooks/`.

## Near-Term Work

1. **Done 2026-11-24 — narrow `main.py`, HTML shells, narrower tests.** Delivered by the refactor above. The HTML shells were already small; the real hotspots were `websocket_session.py` (now split into mixins) and the duplicate mic-capture code in `index-app.js` (now shared `voice-capture.js`).
2. **Done — cross-host failover.** Every role now spans two hosts, the main chain three. The remaining two-hosts-down hole (lilripper + workhorse strands consults/reader/summaries; lilbuddy + lilripper strands image turns) is **deferred by Dave**.
3. **Done 2026-08-26 — `OCTAVIUS_LR_API_KEY`.** The token lives only in `~/.config/secrets.env` and the unit exports it; see `docs/runbooks/config-and-secrets.md`. (The old adoption how-to is in the archive.)
4. **Transcription/dictation mode** (Dave, 2026-07-12): capture speech (Android app first), transcribe, and save the transcript to the `saved_items` stash — deliberately NOT the vault. No agent turn, no TTS. Revives the stash write path for non-note payloads; needs a WS message or REST route for STT-to-stash (the unwired `save_to_stash` helper in `local_tool_inbox.py` is a starting point). App-side sketch in `../octavius-android/docs/HANDOFF.md` NEXT WORK #3.
5. **Reader chat: per-document hybrid search** (Dave, 2026-11-01; deferred). The pre-turn search and `search_reader_document` are word matching with light stemming and a small synonym list (`local_tool_reader_lookup._rank`), which misses paraphrase. Idea: when the panel opens, embed the document's chunks (bge-m3) into a throwaway in-memory index and fuse it with the word ranking; near-instant for one document. Only worth it above a size threshold (a 50-line note no; a 50-page PDF yes). Not via `hybrid-corpus`, which is for persistent corpora.
6. **Reader Original view edges** (shipped 2026-11-04), none urgent: figures show as `[Figure: caption]` because docproc's markdown has no image links (`docs/HANDOFF-docproc-images.md`); the 12 documents converted before 2026-10-31 have no `source_text` and show only reading text until retried (Dave chose to leave them); playback highlights the whole chunk in Original view (Dave's call, revisit if it reads badly); a click inside math picks its sentence by relative position, so it can land one sentence off.

## Planned: `deep_research` domain via headless pi (2026-08-08)

Use `pi --mode json -p --no-session` for a new **async** `deep_research` domain only; keep the inline quick domains (email/tasks/research) on `subagent.py`.

- **Why not inline domains**: warm MCP sessions are the latency story (`run_subagent` borrows the main `MCPManager`'s sessions; a headless pi call is a cold process plus an MCP handshake per server, per call). Shelling out also bypasses `SubagentDispatcher` capacity accounting unless the ticket is wrapped around the subprocess (doable, see `run_inline_subagent`), loses the `===TOOL DATA===` block (`_compose_result`) and per-tool `status_callback` progress, which would have to be re-derived from pi events.
- **The original motivation is weaker than it looked**: MCP tool schemas already flow through dynamically (`mcp.get_tools_for_servers()`). What doesn't auto-update is the per-domain prompt tuning in `SUBAGENT_DOMAINS`, and moving to pi relocates that rather than removing it.
- **Why it fits `deep_research`**: the async path (`delegate_task` / `pull_delegation` / `list_pending_delegations` / `cancel_delegation`, the badge, `spawn_delegation` / `_run_and_announce`, `proactive_speak`, `delegation_*` WS messages; now in `ws_delegation.py`) exists and is merely unregistered; startup cost is noise against a multi-minute run; pi already has a parallel `deep_research` orchestrator.
- **Sketch**: a `deep_research` local tool spec → `spawn_delegation` → `_run_and_announce` shells out, parses the final assistant `message_end`, announces through the badge/pull lifecycle; keep the dispatcher ticket around the subprocess. Not started.

## Known rough edges (unchanged, not scheduled)

- `/health` `degraded` latches until restart (`terminal_failures` is a lifetime counter).
- `end_async` awaits summary, tags and memory push on the WS control path, so New Chat / reset / attach can stall behind them.
- `tools.py` runs sync local-tool handlers directly on the event loop.
- `find_similar_responses` (email) was dead as of 2026-08-14: it resolves the sent folder from an empty `folders` table (mcp-tools side; not re-checked).

## Remaining hotspots

- `pi_brain.py` (1443 lines): process manager, failover, warming, confirm relay and turn streaming in one module.
- `static/reader-app.js` (1994 lines).
- `static/index-app.js` (~1400 lines).

## Migration Note

- `reader_documents.saved_item_id` is still enforced as a plain foreign key. If inbox deletion should eventually null out that reference automatically, that will require a real SQLite migration to add `ON DELETE SET NULL`, not just a schema-file edit.

## Related Design Work

The Android companion app is a working client (`../octavius-android`); `octavius-android-design.md` holds the original design exploration.
