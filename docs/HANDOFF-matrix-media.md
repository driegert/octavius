# HANDOFF — Matrix media (images + PDFs) for Octavius

_Last updated 2026-07-02. Resume point for the media work spanning octavius +
matrix-agent-sidecar. Contract: `docs/ws-media-contract.md`._

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

## Two design changes AGREED, not yet implemented

1. **PDF transport swap (docproc REST client is a dead end).** Discovery:
   `:8201/:8251` hosts DIFFERENT MCP servers per machine — triplestuffed = evangeline
   (email), **lilripper = Document Processing** (`http://lilripper:8251/mcp`, live,
   tailnet-reachable via Caddy). The `:8210` docproc-web REST API octavius targets is
   loopback-only on lilripper AND takes lilripper-local `source_path` (no upload) —
   unusable from triplestuffed. **Fix:** use
   `mcp-tools/server_documents_wrapper.py` (stdio MCP, built for exactly this):
   scp-uploads PDF to lilripper, calls `convert_pdf_to_md` at lilripper:8251,
   downloads .md/meta back to LOCAL paths. Async: `convert_pdf_to_md` → job id,
   `get_conversion_result(job_id)` polls. Octavius's `mcp_manager` already supports
   stdio + programmatic `call_tool`. Plan: register wrapper in `.mcp.json`
   (command = `mcp-tools/.venv/bin/python server_documents_wrapper.py`, cwd
   mcp-tools), rewrite `docproc_client.py` to those two calls, repoint
   `check_document_status`, drop/repurpose `OCTAVIUS_DOCPROC_URL`, update tests.

2. **Vision-chain stickiness per thread (Dave's ask).** Current: only the
   image-bearing turn uses 8010; conversation reverts to 8020 after. Wanted: once a
   thread receives an image, the REST OF THAT THREAD stays on the vision chain and
   keeps the image content-array in the in-memory conversation so follow-up turns
   can reference the image. Design sketch: conversation-level `has_images` flag →
   chain selection; keep content arrays in memory (llama.cpp prefix cache absorbs
   re-sent image tokens); persisted history still stores placeholders (trust
   boundary unchanged). Wrinkle to decide at implementation: after idle-drop +
   thread re-attach, history reload yields placeholders only — optionally
   re-hydrate the content array from the spool path (file persists on disk;
   store path in the attachments table or parse from placeholder), else the
   reloaded thread degrades to text + placeholder. Recommend: re-hydrate if the
   spool file still exists, silently degrade otherwise.

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
