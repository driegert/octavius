# Octavius on pi: design

Status: **steps 1–3 built 2026-10-30 (see their result sections); the rest is
proposal**, revised after review (Codex gpt-6.1-sol, local GLM-5.3-Flash). Measurements in this note
were taken 2026-10-28..30 on the live fleet. Every timing run used synthetic or
trivial prompts, or reported numbers only, so no mail or calendar content was
ever read.

## Decision

Octavius stops running its own agent loop. It keeps everything that makes it a
voice assistant — WebSocket protocol, STT, VAD, TTS, history DB, reader, inbox,
Matrix/Android media contract — and hands each turn to a long-lived **pi**
process (`pi --mode rpc`) running a dedicated `octavius` profile.

Why:

1. **One agent stack, not two.** Today every capability is built twice:
   `agent.py` + `subagent.py` + `consult_specialist` in Octavius, and
   `pi_harness` for everything else. The calendar server (mcp-tools
   `server_calendar.py`, :8258) is the example — pi has it, Octavius does not.
2. **First-turn latency** has been the main source of friction. Measured below:
   a warm pi turn on direct tools is ~7 s end to end, and with on-demand cache
   warming the prefill part of a turn drops from 6.8 s to 0.7 s.
3. **Fewer hops.** The fastest turns on both systems were the ones with the
   fewest model rounds. The profile puts the daily-use tools directly on the
   main agent instead of behind a specialist.

## What the measurements say

All on lilbuddy `gemma4-26b-a4b` (main) / triplestuffed `gemma4-12b` (subagents).

| Path | Time to spoken answer |
|---|---|
| Octavius main → `consult_specialist` (email) | 6.8–13.0 s |
| Octavius main → `consult_specialist` (calendar) | 5.8–23.7 s |
| pi main → `email_finder` subagent | 25–31 s |
| pi main → calendar tool (via `tool_search`) | 7.1 s ×2 |

Subagent hops are the cost (pi's `email_finder` alone took 14–25 s). Tools the
main agent calls directly are fast and consistent.

**Prefill (cold KV cache), lilbuddy 26B**: ~0.65 s per 1k tokens, slightly
super-linear. 7k → 4.0 s, 14k → 8.8 s, 18k → 12.2 s. **Warm: 0.18 s at any
size.** So prompt size only matters on a cold cache — which is what warming fixes.

**Tool schema cost** (gemma4 tokens): email 5,565 (11 tools), calendar 1,625,
vault 490. pi main's base prompt is ~10.1k. The profile below lands at ~12–14k.

## The `octavius` pi profile

A new agent name in `pi_harness/extensions/mcp-scope.ts`, selected by env (e.g.
`PI_AGENT_PROFILE=octavius`) rather than `PI_SUBAGENT`, because it is a main
agent, not a child. All tools **direct** — no `tool_search` round (it cost
~2–2.4 s per turn in the measurements).

| Family | Tools | Notes |
|---|---|---|
| email | `hybrid_search`, `keyword_search`, `get`, `get_many`, `stats` | ~2.1k tokens instead of 5.6k. Drop `semantic_search` (hybrid covers it), `extract`, `find_similar_responses`, `find_people`, `list_conversations` — add back individually if missed. Per-tool trimming uses `toolExposure: hidden`, which `mcp-scope.ts` already does for main's openalex. |
| calendar | `agenda`, `find` | `health` hidden. |
| vault | `search` | Read-only search; note writes are a parity item below. |
| web | `web_search`, `read_url` | pi's `web-search` and `crawl4ai-reader` extensions. Direct, not a subagent: a "when does X close" question should not pay a 15 s hop. |
| tasks | `search_tasks`, `get_task`, `create_task`, `update_task`, `add_task_comment` | Octavius's `tasks` specialist (Vikunja, `subagent.py:193`) must not silently disappear. Full server is 3.6k tokens; this set is ~2.0k. The default-project rules from `subagent.py` move into the profile prompt. Direct, confirmed by Dave 2026-10-30. |
| subagent | `scholar` only | Long-running and tool-heavy; replaces the `research` specialist. Future: `deep_research`. |

pi's coding tools (`read`, `bash`, `edit`, `write`, …) are off. An explicit
`--tools` allowlist excludes every MCP tool not named, so the profile must name
each tool, the same way subagents do.

**"scholar only" must be enforced in code, not by the tool list.** pi's
`subagent` tool takes any agent name and discovers the whole roster at call
time (`extensions/subagent/index.ts`, `execute` → `discoverAgents`), so
exposing it exposes every agent. Add an allowlist (e.g.
`PI_SUBAGENT_ALLOW=scholar`) checked in `execute`. Also: `agents/scholar.md`
lists `lb-unified/gemma4-26b-a4b` as its first fallback, which would put a
child on the voice slots — the octavius profile overrides scholar's fallbacks to
skip lilbuddy.

The email prompt rules Octavius learned the hard way (folder is a closed set of
five, case-sensitive; zero results on a folder means a suspected typo; pass
`folder` explicitly on every call) move into the profile's system prompt.

## Cache warming: `/warm`

### Verified behaviour (2026-10-30)

llama.cpp keeps the processed prompt prefix on a slot. A request that repeats
it with `max_tokens: 1` re-populates the slot; the next real request then only
prefills its new tokens. lilbuddy has **2 slots**, shared with the vision role.

Overlap test, 13.5k-token prompt:

| Scenario | Wait after send |
|---|---|
| No warm-up | 8.5 s |
| Warm-up in flight, real request sent anyway | **14.5 s** — lands on the other slot, both compete |
| Warm-up in flight, real request waits for it | 6.1 s (remainder + 0.36 s) |
| Warm-up finished | 0.34 s |
| Warm-up aborted, then real request | 12.9 s — partial prefill is discarded (`cache_n=0`) |

Prototype `/warm` pi extension (replays pi's last provider payload with
`max_tokens: 1`), driven over RPC with pi main (10.1k prefix):

| Step | Result |
|---|---|
| Turn after both slots evicted, no warm | 6.8 s, 10,150 tokens prefilled |
| `/warm` after eviction | 6.9 s (in the background) |
| Turn after that `/warm` | **0.72 s total**, 24 tokens prefilled, 10,151 from cache |

pi has its own `CacheWarmer` (`cacheWarming` setting), but it is TTL/timer
driven and gated on dollar savings — a local model costs $0, so it never fires
without an extension override, and it cannot be triggered on demand. Not used.

### Requirements for the extension

1. **Capture**: record the last serialized provider payload
   (`before_provider_request`) and headers (`before_provider_headers`).
2. **Replay**: `/warm` POSTs that payload with `stream: false`,
   `max_tokens: 1`, `cache_prompt: true` (and no `stream_options`) to the
   model's `baseUrl`. The replayed payload ends at the previous user turn; the
   tail (last assistant reply + new message) is a few hundred tokens.
3. **Single flight**: at most one warm in flight; a second `/warm` while one
   runs is a no-op.
4. **Real requests wait, with a bound**: `before_provider_request` awaits any
   in-flight warm before returning, so the real request doesn't take the other
   slot (the 14.5 s row). The wait is capped (≈ 20 s, above the ~12 s worst
   measured cold prefill); past the cap, or on any warm error, the real request
   proceeds as if no warm existed. Warming is an optimisation and must never be
   able to fail or hang a turn. The warm `fetch` gets its own timeout too.
5. **Don't abort a warm** just because the user abandoned the message — an
   aborted warm discards its prefill and still competes for the GPU (the
   12.9 s row). The timeout in 4 is the only cancellation.
5a. **Replay only to the endpoint that produced the payload**: store the
   payload with its model and `baseUrl`; after a model switch (failover,
   below) the stale payload is dropped, not replayed to the new host.
6. **Fresh session**: there is no payload to replay before the first request.
   Persist a per-profile template (the payload truncated to system prompt +
   tools) and replay that. The system prompt must therefore be byte-identical
   across sessions of the same profile and channel (date changes once a day —
   `current-date.ts` — which is acceptable).
7. **Observability**: log `prompt_n`, `cache_n`, prefill time per warm, so a
   warm that keeps prefilling the whole prompt (cache never holds) is visible.

### Triggers (Octavius side)

| Event | Why |
|---|---|
| Octavius start; lilbuddy back up (`/health` recovery) | Warms the static prefix (system + tools) |
| `stt_start` (mic opens) | Existing hook (`handle_stt_start`) |
| New `typing_start` WS message (PWA, Android, Matrix sidecar), debounced | Text turns |
| `restore_session` / `load_conversation` | Re-attached thread has history the slot may not hold |

Speech or typing usually lasts 2–5 s. That covers the conversation-history
delta, not a fully cold 12 s prefix — which is why the static prefix is kept
warm separately. Residual worst case: a model reload followed by a message
within ~10 s.

**What `/warm` can and cannot warm.** It replays the last payload *this pi
session* sent. A live process re-attached by `restore_session` has one, so
its whole history warms. A **new or reseeded session** (after
`load_conversation`, a crash, or reaping) has none: the template warms only
system + tools, and the seeded history prefills cold on the first turn. Two
mitigations: reap late (hours, not 30 min — a single user's idle process is
cheap), and a later spike on seeding history *without* a model turn so a
history-bearing payload exists before the first prompt. Until then, that
first-turn cost is stated, not hidden — measure it at realistic thread sizes
(today's `Conversation` keeps up to 40 messages).

The template-only warm was measured in step 3: the real profile's static prefix
is ~7.5k tokens, not the 12–14k estimated, and a template warm caches 7,477 of
it (see "Step 3 result").

### Rules the rest of the design must keep

- **Nothing per-turn goes in the system prompt.** Octavius today folds the
  memory block and the channel style directive into `messages[0]` every turn
  (`agent.py::_build_memory_block`); that would invalidate the cache on every
  turn. Memory and the channel style directive both go into the **user
  message** of the turn they apply to (below).
- **Don't let other work evict the voice slot.** Subagents stay off lilbuddy
  (scholar's fallback list is overridden, above). Vision shares the box; image
  turns are rare enough to accept a re-warm.

## Process model

- **One pi process per active conversation**, started lazily, reaped after an
  idle timeout of hours (see "What `/warm` can and cannot warm"). Single user,
  so in practice one or two processes.
  The process manager owns the `conversation_id → process` map and never lets
  two sockets drive one process at once (a second attach waits or takes over).
- **SQLite stays authoritative.** Octavius's history DB is the record the UI,
  search, summaries and Matrix read; the pi session (`--session-dir`, 0700,
  under Octavius's state dir) is a working cache of the model's context. If a
  pi session is missing or a process crashed mid-turn, Octavius starts a fresh
  pi session and seeds it from SQLite (a compact transcript of recent turns in
  the first prompt) rather than trying to repair pi's file. A turn that failed
  before `agent_settled` is recorded as failed, not half-written.
- **`load_conversation` keeps today's semantics**: it starts a *new*
  conversation row seeded from the old one (`websocket_session.py`
  `handle_load_conversation`), so on pi it starts a new, seeded session — no
  fork/resume of pi's file needed. `restore_session` (same conversation,
  reconnecting socket) re-attaches to the live process, or seeds as above.
- **Channel style is per turn, as today** (`agent.py::_style_directive`): a
  thread can mix voice and text. The directive rides in that turn's user
  message, so the system prompt — and the cache — stay identical for both.
- Profile MCP servers are HTTP (email, calendar, vault), so per-process cost is
  connections, not the ~870 MB of stdio servers pi main starts.

## Turn flow

1. Octavius receives text (STT result or `text_input`), records the user message.
2. Sends `{"type":"prompt","message": <style directive + memory context + text>}`
   over RPC (with `images` for image turns).
3. **Speaking rule (phase 1 = today's behaviour)**: text deltas of each
   assistant message are accumulated; at that message's `message_end`, if it
   carries no tool calls it is the answer — split into sentences and queued for
   TTS; if it carries tool calls its text is discarded. That is exactly what
   `agent.py` does now (sentences buffered, yielded only after the stream ends
   with no tool calls), so phase 1 neither gains nor loses streaming latency.
   Speaking before `message_end` is a later optimisation with its own
   commit/discard rules. The RPC stdout reader runs independently of the TTS
   queue so a slow synth never back-pressures pi. Reasoning arrives separately
   and is never spoken.
4. Tool progress (`tool_execution_start`) → the existing `status` line.
5. On `agent_settled`, record the assistant message; summaries/tags run at
   conversation end exactly as now.
6. Barge-in (`handle_stt_start` during a reply) → RPC `abort`. The DB records
   the turn as interrupted, keeping only text already sent to TTS; whatever pi
   kept of the aborted message stays in its session (it is the model's own
   context of what it was saying).
7. Octavius owns its pi children (one process group): on Octavius shutdown or
   crash they are killed, never orphaned to write session files the DB never
   records. On restart, conversations reseed from SQLite.

**Guardrails to re-establish in pi**, checked in the profile spike (rollout
step 1) rather than discovered in the side-by-side: the 4000-char tool-result
cap, the tool-round cap and "answer now" nudge (rounds 5–6 of 7), image
persistence for follow-up questions (`Conversation.has_images` keeps the image
content in context today; pi_harness has `image-context-trim.ts`, which may
do the opposite), and context growth — pi auto-compacts rather than trimming
to 40 messages, and the cold-prefill worst case grows with it.

What pi 1.4.0 does, checked 2026-10-30 (`dist/`, `docs/`):

| Guardrail | pi | Phase-2 action |
|---|---|---|
| Tool-result cap | MCP text results capped at 20 KB, middle removed (`MCP_OUTPUT_MAX_BYTES`, hard-coded); built-ins 2000 lines / 50 KB | 20 KB is ~5k tokens per result. Add a `tool_result` cap to the profile if turns bloat |
| Round cap | None: the agent loop is `while (true)` | Octavius counts `turn_end` and sends `abort` at 7; the nudge becomes a `steer` at 5 |
| Images | Stored in the session and re-sent every request until compacted; resized to ≤2000 px at intake | Same as today's sticky `has_images`; `image-context-trim.ts` (keep last 6) is on |
| Compaction | Auto, when context > `contextWindow` − 16384; keeps the last 20k tokens | `lb-unified/gemma4-26b-a4b` declares 147,456, so it fires. Same behaviour in RPC mode |
| Final vs tool message | assistant `message_end` with `stopReason` `"stop"` vs `"toolUse"` | The speaking rule above keys on this |

## Step 1 result (2026-10-30)

Built in pi_harness: `scripts/pi-octavius` (one argv for a terminal and for
`--mode rpc`), `profiles/octavius/system.md`, the `octavius` profile in
`extensions/mcp-scope.ts`, and `PI_SUBAGENT_ALLOW` / `PI_SUBAGENT_SKIP_PROVIDERS`
in the subagent extension. Measured live:

- 16 tools declared, the same on every request: 4.5k chars of system prompt and
  28k chars of tool schemas.
- `web_search` turns: about 6 s cold, and 1.4 s to the tool call once the
  cache is warm.
- A `searcher` request is refused. `scholar` runs, but it took 228 s and
  returned the wrong venue for Thomson (1982). That is a scholar quality issue
  to look at separately.
- **Prefix stability needed two fixes.** pi writes the cwd into the system
  prompt, so the launcher pins it. MCP servers connect in a race, so the
  profile sorts the payload's tools by name. Without both, separate
  conversations never share a cached prefix.
- `model-profiles` was stripping `web_search` from turn 2 on (it is
  "profile-only" for models with no per-model profile). It now stands aside
  whenever `PI_AGENT_PROFILE` is set.
- **Prompt-injection guard** (`extensions/octavius-guard.ts`, added after a
  review flagged both gaps). The profile pairs mail with the open web, so an
  instruction planted in an email could have sent mail text out in a fetched
  URL. Once any email/calendar/vault/vikunja result is in the session,
  `read_url` opens only URLs from a `web_search` result or a user message, and
  `scholar` cannot be started. `web_search` stays open (the query reaches the
  provider, not the attacker; the prompt forbids private text in it). Cost: a
  conversation that has read mail cannot start research. Vikunja writes now
  need `ctx.ui.confirm`, which over RPC is an `extension_ui_request` that
  **step 2 must answer** (spoken yes/no on voice, a button or reply on text);
  no answer in 60 s, or no UI, blocks the write.

## Step 2 result (2026-10-30)

Built behind `OCTAVIUS_BRAIN=pi` (default `native`): `pi_brain.py` (RPC process
per conversation, manager, `stream_pi_turn`), wired into `run_turn`, reset,
load, restore and shutdown; tests against a scripted fake pi. Not yet done from
the step-2 list: `/warm` (step 3), and a measured first-*audio* number through
Phonon, as opposed to first sentence. (Both since done: see "Step 3 result" and
"First audio through Phonon".)

Live, synthetic prompts, lilbuddy 26B, no warming, memory injection off:

| Turn | First sentence |
|---|---|
| Trivial, fresh process (includes pi start + MCP connect) | 5.15 s |
| `web_search` turn (tool call at 1.7 s) | 4.10 s |
| Warm follow-up | 2.33 s |
| Vikunja create → confirm asked | 3.14 s; declined, not written, model said so |

Open: Android and the PWA treat a second `audio_done` within one turn (after a
confirm) as untested; the browser ignores `confirm_request`.

## Step 3 result (2026-10-30)

Built: pi_harness `extensions/octavius-warm.ts` (suite `scripts/test-octavius-warm.ts`,
14 cases against a stub llama.cpp server) and the Octavius triggers in `pi_brain.py`
(`PiProcessManager.request_warm` / `warm_static_prefix` / `schedule_recovery`), wired
into `stt_start`, a new `typing_start` frame, `load_conversation`, `restore_session`
and startup. `/health` → `pi_brain.warm`. How the requirements landed:

- **Capture** keeps each agent-loop payload as *pending* and promotes it only on a 2xx
  `after_provider_response`, so a request another extension aborted (remote-model-guard)
  is never replayed. Headers are attached at promotion (pi assembles them after the
  payload hook). Payloads are kept with model and baseUrl; a different model drops them (5a).
- **Replay** returns at once: pi awaits a command handler before replying, so `/warm`
  starts the fetch and returns, and the RPC reply is `handled` in ~10 ms (0.6 s when it
  is a new process's first command). If pi answers anything but `handled` (extension not
  loaded, so `/warm` became a model prompt), Octavius aborts that run and counts it as
  `unsupported`.
- **Single flight, skip mid-turn, wait capped at 20 s, own 60 s fetch timeout, no
  other abort**: as specified. Octavius also skips a warm while it holds the process's
  turn lock. **Never to a provider outside `localProviders`** (private-data.json),
  failing closed. That rule is extra to the list above.
- **Template** (6): `$PI_OCTAVIUS_STATE/warm-template.json`, 0600, rewritten when the
  static prefix changes. It holds the system (`developer`) message, the tools and a
  one-character user turn, with no history and no headers. Startup and recovery warm through a short-lived
  `--no-session` process, or through the most recent live idle process when there is one.
- **Observability** (7): the extension reports `setStatus("octavius-warm", JSON)` with
  `prompt_n`, `cache_n`, `prefill_ms`, `wall_ms`, and a `wait` report when a real request
  waited. Octavius logs each one and keeps counts and the last result for `/health`.
- **Recovery** is the warm itself: a warm that errors or times out, or a failed pi turn,
  starts a retry loop (60 s, doubling to 15 min) that stops at the first `ok` warm.
  There is no separate lilbuddy probe.
- **A pre-started process is still seeded.** `stream_pi_turn` used to seed only a process
  it had just started. It now seeds any process with no turns yet, because triggers now
  start processes ahead of the turn.

Measured live, synthetic prompts (arithmetic, trivia, one `web_search`), memory block
stubbed, real `pi-octavius` (isolated state dir) on lilbuddy `gemma4-26b-a4b`. Before every
run both lilbuddy slots were overwritten with synthetic filler, so "cold" means nothing
cached. Three runs each, first sentence from the moment the turn is sent; `prefilled` is
the turn's own uncached input tokens.

| Turn | Cold | Warmed (warm finished first) | Warm overlapping 3 s of "speech" |
|---|---|---|---|
| New conversation (pi start included in cold) | 7.8–8.3 s (7,574 prefilled) | 0.5–2.5 s (97 prefilled; template warm) | 3.5–5.3 s (waited 1.9 s for the warm) |
| Follow-up in a live process (3 earlier turns) | 6.2–6.6 s (7,816) | 2.5–3.0 s (105; session warm) | 3.7–4.4 s (waited 1.7 s) |
| `web_search` follow-up | 7.6–9.0 s (7,665) | 3.4–3.9 s (118) | — |
| Reseeded 40-message thread (seed capped at 6,000 chars) | 10.0–14.9 s (8,878) | 3.4–11.0 s (1,402; template warm) | — |

- **A warm costs ~4.45 s** for the 7.5k static prefix (template) and ~4.7 s for a short
  session (prompt_n 7.5–7.7k, cache_n 1). That is ~0.6 s per 1k tokens, the same rate as
  the cold prefill it replaces. 18 of 18 warms were `ok`, and no wait hit the cap.
- **3 s of speech does not cover a fully cold prefix.** The real request waits out the
  remaining ~1.8 s of the warm, as designed. That still halves first-sentence latency
  (≈8 → ≈4 s), because the waiting turn then prefills ~100 tokens instead of 7.5k.
  The doc's two-stage plan holds: keep the static prefix warm separately (startup,
  recovery) so the mic-open warm only has the conversation delta to cover.
- **What remains after a warm is the model's thinking**, not prefill: 0.5–3 s on these
  prompts, so the spread in the warmed column is generation, not cache.
- **Reseeded threads stay partly cold, as predicted.** The template covers system + tools,
  and the seeded transcript (~1.4k tokens) prefills on the first turn. The 11.0 s run
  was a long thinking pass: the turn's prefill was the same 1,402 tokens as the
  3.4 s run.

Open from step 3:

- **No client sends `typing_start` yet** (PWA, Android, Matrix sidecar). The server
  side exists, with a 5 s per-conversation debounce.
- The template carries the day's date (`current-date.ts`). After midnight, a startup or
  recovery warm replays yesterday's prompt until the first real request rewrites it, so it
  re-caches only the part before the date line.
- A template warm for a new conversation can land on the slot holding another live
  conversation's history and truncate it to the shared prefix. Harmless for one user;
  revisit if two conversations alternate.
- A template warm sends no `Authorization` header (headers are never persisted). That is
  fine for lilbuddy (open); a template warm to an authed failover host (`:8020`) would 401
  until a request from that session supplies one. Matters at step 4.
- ~~First-audio latency through Phonon~~: measured, see "First audio through Phonon".
- Re-check `/warm` after every pi upgrade: it depends on `before_provider_request`
  chaining, `after_provider_response`, command dispatch in `prompt`, and RPC `setStatus`.

## First audio through Phonon (2026-10-30)

First audio is measured from "turn sent" to the first WAV being ready. That equals the
first sentence plus the Phonon synth of that sentence, because `run_turn` synthesizes each
sentence inline as it arrives and then sends it. Setup:

- Both brains were driven directly, with no WebSocket, history DB or live service, and the
  memory block stubbed. Synth went through `tts.synthesize` (speechify plus the real client),
  to Phonon 31m at `lilripper:8881`, voice `Imogen`.
- Native used the real tool schemas for its five servers plus the local tools (24 tools in
  all). Only `web_search` actually ran; every other tool call was refused.
- Cold means both lilbuddy slots were overwritten first. Warmed means the cache was filled
  before the turn. For native that was a priming turn with the same system prompt and tools.
  For pi it was a `/warm` template warm, finished before the turn.
- pi's cold column includes pi start-up, because it is a new process.
- Prompts were synthetic only (arithmetic and trivia, plus one `web_search` lookup), three
  runs each. Cells show the range and the median.

| Turn | Native cold | Native warmed | pi cold | pi warmed |
|---|---|---|---|---|
| Trivia | 8.9–14.6 s (10.6) | 0.9–2.7 s (2.6) | 6.8–17.7 s (11.0)² | 0.7–4.0 s (2.6)¹ ² |
| `web_search` | 15.2–17.9 s (17.8) | 5.3–8.4 s (6.7) | 7.6–9.9 s (8.9) | 2.2–6.6 s (4.0)² |
| Phonon share of that | 0.16–0.21 s (4–7-word first sentence) | | 0.24–0.46 s (9–21 words) | |

¹ One more warmed run took 11.9 s. That run did not record token usage. In the rerun, every
warmed turn prefilled 96–110 tokens with 7,477 cached, so what varies after a warm is the
model's thinking (and, in one cold run, a second model call), not prefill.
² Six runs: the first pass plus that rerun.

- **Phonon is not the bottleneck.** It is 2–5 % of a warmed turn's first audio and under 3 %
  of a cold one. The LLM's prefill and thinking are everything else.
- **pi's `web_search` turns beat native's**, cold and warm, at about half the time. Native
  pays a second full round with the 24-tool payload plus its own memory and style folded
  into the system prompt. pi's second round reuses the cached prefix.
- On a trivia turn the two brains are level once warm. pi's cold start pays process start
  (~0.6 s) on top of prefill.

Phonon microbench, 10 sentences of 1–31 words through `tts.synthesize`, three passes:

| Pass | p50 | p90 | max |
|---|---|---|---|
| 8 s idle before each request | 0.31 s | 0.38 s | 0.44 s |
| back-to-back (2 passes) | 0.27–0.28 s | 0.39–0.40 s | 0.44 s |

Synth time is roughly 0.09 s + 0.011 s per word: 0.10 s for one word, 0.15 s for five,
0.43 s for 31. Idling makes little difference, and there is no cold-start penalty worth
keeping warm for. A reply's later sentences synthesize while the earlier ones play (a
5-word sentence is ~3 s of audio against ~0.15 s of synth), so only the first sentence's
synth reaches the listener. Harness: `scratchpad/step3/first_audio.py` (not in the repo).

## What Octavius's own tools become

The main agent has 17 local tools today. Phase 1 ships without the ones marked
*later*; the native path stays available until parity.

| Tools | Plan |
|---|---|
| `consult_specialist` | Gone — replaced by direct tools + `scholar`. |
| `save_note`, `read_note`, `edit_note`, `commit_edit` | Expose from Octavius as a loopback **`octavius` MCP server** (same code, `vault_files.py` contract unchanged). *Phase 1.* |
| `remember`, `forget`, `correct`, `what_do_you_know` | Same loopback server (they wrap the memory service). *Phase 1.* |
| `search_conversation_history`, `read_conversation` | Same loopback server. *Phase 1* — Matrix's "continue a past voice conversation in text" is built on them. |
| `read_document`, `list_reader_documents`, `append_to_reader_document`, `process_pdf`, `check_document_status`, `download_file` | Same loopback server; reader state stays in Octavius. *Later.* |

Item chat (`/inbox`) and PDF/media turns keep the native path until the main
voice/text path is proven.

## Failover

Today the main chain has three hosts (lilbuddy → lilripper :8020 → triplestuffed).
pi's main agent has one `--model`; `subagent/fallback.ts` covers children only.
The new path must not ship with less availability than the old one, so
failover is a **gate before the default flips**, not an open item.

Approach, in order of preference: (a) a pi virtual model
(`registerVirtualModel`) that fails over per request inside pi — preferred
only once a spike shows it surfaces connect/timeout/5xx failures and lets the
session continue; (b) Octavius switches model over RPC (`set_model`, no
restart) when a turn fails. Neither keeps the cache: the fallback host starts
cold, so failover turns pay a full prefill (~0.65 s per 1k tokens on lilbuddy;
unmeasured on `:8020` and triplestuffed) until the primary returns — measure
that path too. Either way:

- Trigger on the same classes `service_clients.classify_chain_error` already
  distinguishes — connect, connect-timeout, generation timeout (the "zombie"
  that lists but never generates), 5xx, auth (401), and missing alias (400/404)
  — each with a bounded timeout, never pi's default.
- Image turns fail over only to image-capable hops (lilbuddy → `:8020`).
- A failure *after* a tool has executed must not re-run that tool: resume from
  pi's session (which already holds the tool result), don't replay the turn.

The chain order mirrors today's: lilbuddy 26B → lilripper `:8020` qwen3.8-27b →
triplestuffed gemma4-12b.

## Rollout

1. `octavius` profile in `mcp-scope.ts` + its system prompt + the subagent
   allowlist; checked from a pi terminal first.
2. **Vertical slice** behind `OCTAVIUS_BRAIN=pi|native` (default `native`):
   RPC process manager, event → TTS bridge (speaking rule above), status line,
   barge-in → `abort`, SQLite recording, crash → reseed, and answering
   `extension_ui_request` confirms (vikunja writes). Tests for each, plus
   measured **first-audio** latency per turn type without warming —
   end to end through thinking, sentence split and Phonon, not prefill alone.
3. `/warm` extension in `pi_harness` (requirements above) and the Octavius
   triggers; re-measure. Building it second means its gain is measured against
   a working slice, not assumed.
4. Failover (above).
5. Loopback `octavius` MCP server (notes, memory, conversation history).
6. Flip the default when a written capability checklist passes — every
   current domain (email, calendar via the email specialist today, vault,
   web, tasks, research, notes, memory) has a home or an explicit decision to
   drop it — latency is no worse than native on any turn type, and an image
   turn's effect on the voice slots has been measured.
7. Migrate the remaining native callers of `stream_agent_turn` — item chat,
   PDF/media turns — to pi.
8. Only then delete `agent.py`'s loop, `subagent.py`,
   `subagent_dispatcher.py`, `consult_specialist`, and the main/subagent
   `LLMChainClient` routing.

## Risks and open questions

- **Failover** (above).
- **Privacy boundary**: pi sessions will now hold mail/calendar content on disk
  (`--session-dir`). Keep it local, 0700, and out of anything synced or sent
  to cloud agents — same rule as `evangeline-email` today.
- **pi upgrades** can change RPC events or extension hooks; pin the pi version
  Octavius is tested against and check `/warm` after each upgrade.
- **Model behaviour on direct tools**: the 0.47 s Octavius calendar turn that
  answered without calling a tool shows the model can skip tools. The profile
  prompt needs the same "check before answering" rules, and the side-by-side
  run should count tool-less answers to personal-data questions.
- **Vision on the same slots**: an image turn may evict the voice prefix; the
  next voice turn re-warms. Measure before deciding whether it matters.
