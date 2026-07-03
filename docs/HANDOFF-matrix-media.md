# HANDOFF — Matrix media (images + PDFs) for Octavius

_Last updated 2026-07-02 (evening). Resume point for the media work spanning
octavius + matrix-agent-sidecar. Contract: `docs/ws-media-contract.md`._

## Where we are (all code DONE, nothing deployed yet)

**Repo reorg (2026-07-02, complete):** `~/git_repos/` (→ `~/school_lab/git_repositories/`)
now holds `matrix-server/` (continuwuity deploy + PRD/HANDOFF), `matrix-agent-sidecar/`
(own repo, DESIGN.md adopted), `agent-memory/` (memory extracted from octavius via
git filter-repo; `octavius-memory.service` repointed there and LIVE, 30 facts,
predicates repaired; see that repo's HANDOFF.md). Octavius keeps only
`memory_client.py`; `feat/long-term-memory` merged to `develop` (unpushed).
The old `~/ai_chats/matrix_lxc_temp/` is deleted.

**Sidecar (Rust, Codex-implemented, UNCOMMITTED working tree — review pending):**
`m.image`/`m.file` download+decrypt via `client.media().get_media_content(..)`
(matrix-sdk 0.18.0 decrypts `MediaSource::Encrypted`), spool to `media_spool_dir`
(default `/media/extra_stuff/octavius/matrix_media`, 0755/0644, sanitized
`<ts>_<event_id>_<name>`), caps 20 MB img / 50 MB file, caption rule
(body≠filename), failures/audio/video/stickers degrade to descriptive
`text_input`. Unit file: added `ReadWritePaths` for the spool, fixed
`Documentation=`. `cargo check` / `test` (8) / `build --release` all pass;
frame fields cross-checked against octavius's parser.

**Octavius (branch `feat/matrix-media`, 5 commits incl. merge, 246 tests green):**
- `image_input` handler → OpenAI content-array (base64 data URL) → routed through
  new `vision_llm_chain` (`OCTAVIUS_VISION_LLM_CHAIN`, default
  lilripper:8010 `qwen3.6-35b-a3b-general`); text turns untouched (8020 chain).
  After the turn, image content is downgraded to a `[image: <filename>]`
  placeholder (history/memory never see base64 — trust boundary).
- `file_input` PDF → `docproc_client.py` (REST to :8210) + `check_document_status`
  local tool; caption = instructions (bounded poll ~5 min) vs no caption = ack +
  ask what next.

## Two design changes — IMPLEMENTED 2026-07-02 evening (uncommitted; suite 267 green)

1. **PDF transport swap — DONE.** Discovery that simplified the plan: octavius
   ALREADY registers the mcp-tools documents wrapper as its stdio MCP server
   `document-processing` (`settings.py` `DEFAULT_MCP_SERVERS` →
   `server_documents_voice_wrapper.py`; scp to lilripper → convert at
   `lilripper:8251/mcp` → download .md/meta back to LOCAL paths). No new
   registration needed. `docproc_client.py` rewritten (Codex lane): drives
   `convert_pdf_to_md` / `get_conversion_result` via `MCPManager.call_tool`;
   parses job id / md+meta paths / stage / failure texts; caches terminal
   outcomes (the wrapper deletes a job record on first terminal retrieval) —
   but deliberately does NOT cache poll timeouts or transport-error strings
   (possibly transient), and `submit_job` evicts a stale cache entry when the
   wrapper's per-process id counter reuses an id after a restart. New
   signatures take the MCP manager: `submit_job(mcp, path) -> job_id: str`,
   `poll_job(mcp, job_id) -> dict`, `get_job_status(mcp, job_id) -> dict`.
   `check_document_status` now uses its `_mcp_manager` arg;
   `websocket_session.py` call sites pass `self.state.mcp_manager`.
   `docproc_url`/`OCTAVIUS_DOCPROC_URL` removed; poll/inline/excerpt knobs
   stay. Smoke-verified: MCPManager spawns the wrapper and registers both
   tools. NOTE: the voice wrapper locks conversion to *reading* mode (no
   images/tables in the .md); point the registration at
   `server_documents_wrapper.py` instead if full mode is ever wanted.

2. **Vision-chain stickiness per thread — DONE (Sonnet lane).**
   `Conversation.has_images` (set by `add_user` on list content, cleared by
   `reset()`); `stream_agent_turn` routes on `conversation.has_images`, so
   after the first image the WHOLE thread stays on the vision chain; the
   post-turn `_downgrade()` mechanism is removed — content arrays stay in the
   in-memory conversation (bounded by `trim()`; llama.cpp prefix cache
   absorbs re-sent image tokens). Re-attach re-hydration:
   `history_store.get_conversation_messages` now returns
   `message["attachments"]`; `Conversation.load_from_history` re-hydrates the
   **3 most recent** image attachments (`MAX_REHYDRATED_IMAGES`) from the
   spool file when it still exists (base64, mime from extension), silently
   degrades to the placeholder otherwise, and sets `has_images` only on a
   successful rehydrate. Persisted history / memory extractor still only ever
   see text placeholders — trust boundary unchanged.

Also fixed in passing: `tests/test_websocket_session.py`'s
`test_poll_failure_still_runs_a_turn_reporting_the_error` never executed its
coroutine (missing `asyncio.run`), and its neighbour ran twice. Docs updated:
`CLAUDE.md` (vision + PDF bullets), `docs/ws-media-contract.md` impl notes.

## Deployment checklist (Dave, mostly root)

1. Review + commit the sidecar working tree; then:
   `sudo mkdir -p /media/extra_stuff/octavius/matrix_media && sudo chown octavius-matrix: /media/extra_stuff/octavius/matrix_media`;
   `sudo install -m755 target/release/matrix-agent-sidecar /usr/local/bin/octavius-matrix-sidecar`;
   `sudo cp deploy/octavius-matrix-sidecar.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart octavius-matrix-sidecar`.
2. After the two design changes land: `systemctl --user restart octavius`
   (tree is on `feat/matrix-media`).
3. E2E in Element: image → vision reply; follow-up question about the image
   (stickiness); PDF with caption; PDF without caption → ask-what-next →
   "what's the status of my pdf".
4. Confirm lilripper:8010 `qwen3.6-35b-a3b-general` accepts image content
   (Dave says image capability is enabled).

## Open decisions / loose ends

- Push: octavius `develop` + `feat/matrix-media` ahead/unpushed; agent-memory,
  matrix-server, matrix-agent-sidecar have no remotes yet.
- `unnamed Dell Optiplex` near-dup `works_on`/`uses_tool` facts in agent-memory
  (judgment call; see agent-memory/HANDOFF.md).
- Codex pane (w7:p4) can be closed after Dave reads its report.
- Sidecar tree intentionally uncommitted (herd convention: user reviews).

## Gotchas (don't relearn)

- uv for everything; commit/push only when asked; stay on triplestuffed;
  never `rm` with a variable/glob.
- `~/git_repos` is a symlink to `~/school_lab/git_repositories` — services and
  units use the resolved path.
- Port scheme trap: `<host>:8251/mcp` is a DIFFERENT MCP server per host
  (triplestuffed = evangeline; lilripper = documents).
- Octavius + octavius-memory are LIVE user services on this host; restart only
  deliberately (memory service already cut over and healthy).
