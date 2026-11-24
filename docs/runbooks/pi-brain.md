# pi brain

Read this before touching `pi_brain.py`, `pi_tools_server.py`, the pi launcher/profile, pi failover, cache warming, the agent model menu, or the confirm relay. Design and history: `docs/pi-backend-design.md`.

## What it is

Opt-in via `OCTAVIUS_BRAIN=pi` (default `native`); **live since 2026-10-31** (`OCTAVIUS_BRAIN=pi` in `~/.config/octavius/env`; roll back by setting `native` and restarting).

- Voice and text turns run on a long-lived `pi --mode rpc` process per conversation, launched through `pi_harness/scripts/pi-octavius` (the `octavius` profile: email/calendar/vault/vikunja/web tools direct, `scholar` as the only subagent).
- `pi_brain.stream_pi_turn` has `stream_agent_turn`'s contract; `run_turn` picks one per turn (`pi_brain.wants_pi`). Image turns, vision threads, item chat and file turns stay native.
- SQLite stays authoritative: a fresh or crashed process is seeded with a compact transcript of the in-memory `Conversation`.
- pi has no round cap, so `pi_brain` counts `turn_end`, steers at 5 and aborts at `max_tool_rounds`.
- Processes are reaped after `OCTAVIUS_PI_IDLE_REAP_S` (4 h), killed on reset, load and shutdown, and re-keyed on `restore_session`.
- pi never gets `OCTAVIUS_*` env vars (`child_env` strips them).

## Privacy guard and confirmations

- Vikunja writes are **not** confirmed (since 2026-10-31): pi_harness `octavius-guard.ts` refuses description replacement and over-long writes, and the profile exposes no delete tool.
- The confirm relay stays for any extension that asks: an RPC `confirm` is spoken, sent as `confirm_request` + `audio_done`, and Dave's next transcript or `text_input` answers it (`_spawn_turn`; opening the mic then is not a barge-in). Timeout `OCTAVIUS_PI_CONFIRM_TIMEOUT_S` (55).
- A process seeded from history is marked private for the guard when the conversation read private data earlier (`conversation_read_private` over `tool_calls`, using pi_harness's `private-data.json` beside the launcher, fail closed; the seed carries `TAINT_MARKER`), because the replayed transcript has no tool results the guard could see.
- Reader documents do not taint (Dave's choice), and a Reader chat turn carries no memory block — see [reader.md](reader.md).

## Cache warming

pi_harness `octavius-warm.ts`, RPC prompt `/warm`. `stt_start`, a `typing_start` WS frame (no client sends it yet), `load_conversation`, `doc_chat_open` (these four start the process if needed), `restore_session`, `doc_chat_model` and `chat_model` warm the conversation's process. Startup and recovery (after a failed warm or turn, retried with backoff until one succeeds) warm system + tools through a short-lived `--no-session` process. All fire-and-forget (`PiProcessManager.request_warm`); a process with no turns yet is still seeded. A model only warms once it has a per-model template, i.e. after its first real turn. `/health` → `pi_brain.warm`: counts and last result (`prompt_n` = tokens prefilled, `cache_n` = from cache).

## Failover

Octavius-driven over RPC (`set_model`); pi's own auto-retry is off.

- Hops: `OCTAVIUS_PI_MODEL` (default `lb-unified/gemma4-26b-a4b`), then `OCTAVIUS_PI_FALLBACK_MODELS` (`lrv-p01/qwen3.8-27b,wh-vllm/gemma4-12b`).
- A turn moves on a provider error (5xx, 401, 400/404, refused connect) or when the model is silent for `OCTAVIUS_PI_FIRST_OUTPUT_TIMEOUT_S` (60, before first output) or `OCTAVIUS_PI_SILENCE_TIMEOUT_S` (30, after; thinking counts, tool runs do not). It resumes in the same pi session with a short note; a tool that already ran is not re-run.
- A context overflow moves that turn alone to a bigger-window hop (`OCTAVIUS_PI_MODEL_CONTEXTS`), never touching the shared hop.
- One shared current hop (`FailoverState`): new processes start on it, live ones `set_model` before their turn, and `OCTAVIUS_PI_FAILOVER_COOLDOWN_S` (300) later the next turn tries the primary again. Each failover logs at WARNING; `/health` → `pi_brain.failover`.
- pi's own key for lilripper `:8020` is `PI_AGENT_LR_API_KEY` (pi_harness models.json, provider `lrv-p01`), exported by the service drop-in like `OCTAVIUS_LR_API_KEY`. Without it the `:8020` hop is invisible to pi.

## Model menus

- **Agent menu** (since 2026-11-02): `GET /api/chat-models` → `{models:[{id,label}], default, enabled}`; client `chat_model {model}` (unknown → default) → server `chat_model {model, label, models, default, enabled}`. The choice belongs to the **connection**: `_apply_chat_model` re-applies it (`PiProcessManager.prefer`, keyed by conversation id) after reset, load, restore and before every pi turn, and skips Reader chats. A client that sends `chat_model` also gets `answered_by {model, label}` before each pi reply. Menu: `PiBrainSettings.chat_models` (`OCTAVIUS_PI_CHAT_MODELS`, same shape as the Reader menu); first entry `auto` (`model: null`) is the shared failover chain with thinking on (not the Reader menu's thinking-off 12B, because Dave reverted thinking-off on the agent over answer quality). Web `/` UI detail: [frontend.md](frontend.md).
- **Reader menu** (`OCTAVIUS_PI_READER_MODELS`): see [reader.md](reader.md). `PiProcessManager.prefer` makes a conversation try its chosen model first (hop index `PREFERRED`); a failure there moves the turn to the shared chain without counting against the shared hop. `set_thinking_level` switches thinking per attempt and restores the process's starting level (read via `get_state`) for shared hops. The model that answered (`state.pi_hop`) is what history records (`pi:<model>`).

## Octavius's own tools for pi (`pi_tools_server.py`)

16 local tools (notes, memory, conversation history, reader, reader look-ups; not `download_file`/`process_pdf`/`check_document_status`) are served to pi as the `octavius` MCP server, dispatched through `tools.call_tool`.

- Listens on its own socket, `127.0.0.1:8032` (`OCTAVIUS_PI_TOOLS_PORT`), never on the FastAPI app (Caddy publishes that). Started in the app lifespan (`app_lifespan.py`) only when the pi brain is on.
- Each pi process gets a random token in `PI_OCTAVIUS_TOOLS_TOKEN` (deliberately not an `OCTAVIUS_*` name, which `child_env` strips). `stream_pi_turn` binds it to the turn's `history_session`/`mcp_manager`/session and unbinds in `finally`, so a call outside a turn is a tool error.

## Knobs

`OCTAVIUS_PI_TOOL_TIMEOUT_S` (120; tripled for the `subagent` tool, since `scholar` alone took 228 s) stops a turn whose tool runs past it, with a spoken apology; it is not counted as the hop's fault, so it does not fail over.

`OCTAVIUS_PI_LAUNCHER`, `OCTAVIUS_PI_BIN` (the user service's PATH lacks `~/.local/bin`), `OCTAVIUS_PI_PATH` (prepended to pi's PATH only: the service's PATH finds `/usr/bin/node` 18, which cannot run pi 1.4.0, so the live env points it at nvm's Node 24 — bump it when nvm's version changes; without it every pi start fails with `enableCompileCache` and every warm reports "pi process exited"), `OCTAVIUS_PI_MODEL`, `OCTAVIUS_PI_TURN_TIMEOUT_S` (600), `OCTAVIUS_PI_CONFIRM_TIMEOUT_S` (55), `OCTAVIUS_PI_WARM` (on), `OCTAVIUS_PI_WARM_DEBOUNCE_S` (5), `OCTAVIUS_PI_WARM_RETRY_S` (60), `OCTAVIUS_PI_TOOLS_PORT` (8032), `OCTAVIUS_PI_IDLE_REAP_S`. `/health` → `pi_brain`.

Tests: `tests/test_pi_brain.py` runs against `tests/fake_pi_rpc.py` (a scripted `pi --mode rpc` subprocess); `tests/test_pi_tools_server.py`; `tests/test_chat_model.py`.
