# Octavius

Self-hosted voice assistant running on Dave's homelab. No cloud APIs; runtime traffic stays on the Tailnet.

## Purpose

This file is the high-signal working context for contributors and coding agents:

- what Octavius is
- how to run and validate it
- where the main responsibilities live
- which areas are still risky or incomplete

It is not intended to be a release log. Keep transient change notes brief and move longer design or roadmap material into separate docs.

## Runbook

Install dependencies:

```bash
uv sync
```

Run locally in the foreground:

```bash
uv run python main.py
```

Run via the normal user service:

```bash
systemctl --user start octavius
systemctl --user restart octavius
```

Service endpoint:

- FastAPI binds to `127.0.0.1:8030`
- Caddy exposes it at `https://octavius.riegert.xyz`

### Configuration and secrets

**Nothing loads a `.env` file.** `settings.py` reads `os.environ` directly, so
config only reaches the app through the process environment. `.env.example` is
a reference for variable names and defaults, not a file the app consumes.

- **Service**: `~/.config/systemd/user/octavius.service.d/env.conf`. It does two
  things. `EnvironmentFile=-%h/.config/octavius/env` supplies **non-secret**
  config (mode 0600, outside the repo). The lilripper bearer token is *not*
  there: an `ExecStart=` override sources `~/.config/secrets.env` in a subshell
  and exports `OCTAVIUS_LR_API_KEY` alone, so the token lives in exactly one
  file fleet-wide. After editing either file: `systemctl --user daemon-reload && systemctl --user restart octavius`.

  Two reasons it is a subshell and not simply a second `EnvironmentFile`, both
  verified 2026-08-26: `secrets.env` uses `export FOO=...` lines, which
  systemd's `EnvironmentFile` parser does **not** understand — it logs
  `Ignoring invalid environment assignment` and sets *nothing*, which would
  start Octavius keyless and 401 the primary on every turn, silently. And the
  subshell keeps that file's nine other keys out of this service's environment
  (confirmed: only `OCTAVIUS_LR_API_KEY` appears in `/proc/<pid>/environ`).
  The value never reaches argv, so it stays out of `ps`. Same pattern as
  `notesmd.service`.
- **Foreground**: `set -a; source ~/.config/octavius/env; set +a; uv run python main.py`.

**LLM endpoint auth**: only the two lilripper ports (`:8010`, `:8020`) are behind auth. Two env vars feed
`settings.llm_api_keys`, which `service_clients.auth_headers()` resolves by URL
for every `LLMChainClient` request path (`stream_chat`, `complete`,
`complete_with_tools`):

- **`OCTAVIUS_LR_API_KEY`** (preferred; `LR` = lilripper) — a bare token applied
  to **both** lilripper router ports. This is the value that rotates, so it
  **wins** over the JSON map. The var name is stable across rotations by design.
  It is scoped to lilripper, *not* to a port: `lilbuddy:8010` and
  `triplestuffed:8010` also listen on 8010 and are open, so they must never
  receive the header (`settings.KEYED_LR_ORIGINS` pins this; there are tests).
  **Widened from `:8010`-only on 2026-08-26**, when `:8020` was found to have
  gone behind the same token — with the key pinned to `:8010`, Octavius sent
  `:8020` no header at all and read the resulting 401 as an ordinary failover.
- **`OCTAVIUS_LLM_API_KEYS`** — the general mechanism: a JSON object mapping
  endpoint *origin* (`scheme://host:port`) to a bearer token, e.g.
  `{"http://lilripper:8010":"sk-..."}`. Use it if another endpoint goes behind
  auth. In a systemd `EnvironmentFile`, single-quote the value so the JSON
  survives: `OCTAVIUS_LLM_API_KEYS='{"http://lilripper:8010":"sk-..."}'`.

Endpoints absent from both are called with no `Authorization` header. Keys are
held per origin, not per chain entry, because one endpoint is reached from
several chains — as of 2026-09-10 `lilripper:8010` serves the **main chain
primary**, the subagent and vision *fallbacks*, the reader LLM (reached through
the main-chain client's `urls=` override), and the summary primary; the
subagent and vision *primaries* sit on `triplestuffed:8010`, which is open.
Its position as main-chain primary means an unset or stale key breaks the
*first* hop of every turn, not a fallback.

**This is not hypothetical — it happened.** Until 2026-08-26,
`~/.config/octavius/env` carried a *stale* token inside `OCTAVIUS_LLM_API_KEYS`
and no `OCTAVIUS_LR_API_KEY` at all, while the live token sat in
`~/.config/secrets.env` (which the service does not read). Result: every turn
401'd on `:8010`, 401'd on `:8020`, and landed on the lilbuddy gemma4 hop —
**28 s to the first spoken word instead of 3 s** — while `consult_specialist`,
the vision chain, the reader LLM and summaries (none of which have a third hop)
failed outright and silently. Nothing was "down", so `/health`'s
`endpoints_rejecting_credentials` was the only thing that said so. The duplication is
now gone entirely: `~/.config/secrets.env` holds the token and the unit reads it
from there at start time, so there is no second copy to go stale. Run
`scripts/octavius-models check` before assuming an application bug.

A 401 still burns a failover hop (it is an `HTTPStatusError` like any other),
but as of 2026-08-08 it no longer *hides*: `service_clients.classify_chain_error`
buckets every chain failure into `auth` / `client_error` / `server_error` /
`connect` / `connect_timeout` / `timeout` / `bad_response`, and `/health`'s `llm_chain` reports
`endpoints_rejecting_credentials`, `auth_failures`, `last_failure_kind`, and
per-endpoint `last_error_kind` / `last_error_status` / `authenticated`. A 401
also logs at ERROR naming the origin and which env var to check. `last_error_*`
clears on that endpoint's next success, so a non-null value means "currently
believed broken, this way"; the counters are lifetime. The practical split:
`auth` = key problem; `connect`/`connect_timeout` = host down (no TCP
handshake); `timeout` = host accepted the connection but never generated (a
"zombie" — `/v1/models` answers, completions hang); `client_error` (usually
400/404) = model alias missing from that endpoint's catalog.

### Model routing (`models.json`)

Every LLM role — main, subagent, vision, reader, summary — is routed from one
optional file, `~/.config/octavius/models.json` (`OCTAVIUS_MODELS_FILE` to
relocate). This is the **only** file the app loads; `settings.py` still reads
`os.environ` directly for everything else, and the file holds no secrets — only
which endpoint serves which alias with which generation params.

Precedence, lowest to highest: **code defaults < `models.json` < environment**.
Env still wins so tests and one-off overrides work unchanged; a role absent from
the file keeps its code default. Malformed JSON, an entry with no `url`, or an
empty role list **raises** rather than falling back — restarting onto routing
nobody asked for is the failure mode the file exists to prevent.

Each entry is `{url, model, params?}` (plus `role`/`capacity` for subagent
entries). **`params` merges into the request body**, which is the only way to
reach generation knobs the agent path never builds:

```json
{ "url": "http://lilripper:8010/v1/chat/completions",
  "model": "qwen3.6-35b-a3b-mtp-general",
  "params": { "chat_template_kwargs": { "enable_thinking": false } } }
```

The caller's payload wins over `params` on conflict (a request that deliberately
sets `temperature` is not overridden by routing config); `model` is always the
endpoint's. A non-dict `params` logs a warning and is ignored rather than
raising — bad routing config must degrade, never take a live voice turn down.

**Thinking is the big latency knob.** Measured 2026-08-26 on
`qwen3.6-35b-a3b-mtp-general`: baseline 2.25 s to first content token (378
reasoning deltas); with `enable_thinking: false`, **0.22 s** (0 reasoning
deltas), with tool selection and argument JSON unchanged on both a `web_search`
and a `consult_specialist` prompt. It was set on the main chain's `:8010` entry
for two days and then **reverted 2026-08-28**: the speed was real and tool
selection stayed correct, but Dave did not trust the *answers*. **Every role now
runs thinking on** (the Qwen3 template default, so no `params` are needed for
it); speed is bought on the fallbacks instead. Cost of that choice, measured
end-to-end: first audio ~2.2-4.0 s with thinking, ~1.5-1.8 s without. If it is
ever traded back, prefer `reasoning_effort: "low"`/`"medium"` — a dial rather
than the `enable_thinking` switch. The gemma4 entries (main hop 3, subagent
primary, vision primary, reader fallback) run **uncapped**: `reasoning_effort:
"low"` was tried on them on 2026-09-10 and removed the same day. Measured on
triplestuffed, warm: a trivial prompt answers in ~0.2-0.3 s either way; a real
paragraph-length question takes ~4.6-5.7 s uncapped (~3500-4300 reasoning
chars) against ~3.6-3.9 s capped (~2800 chars). The cap trims a quarter to a
third, not an order of magnitude — gemma4 is fast because it is an A4B, not
because of any dial — so Dave chose the model as-is over a knob that also
shortens the answers.
Beware `max_tokens` on a thinking model: it must budget for reasoning tokens or
the model spends the whole budget thinking and returns empty `content`.

**`scripts/octavius-models`** is the operational front end:

```bash
scripts/octavius-models show     # what routing loads, and from where
scripts/octavius-models check    # probe every endpoint: reachable? alias present? authed?
scripts/octavius-models apply    # check, then daemon-reload + restart + confirm /health
```

`check` exists because the two failure modes that actually bite are both
invisible to reading the config: an alias that has vanished from a router's
catalog (hard-400 on use, logged as an ordinary failover) and a rotated token
(401, likewise). Both were live on 2026-08-26. It exits nonzero on any failure,
so `apply` refuses to restart onto broken routing.

Primary UI routes:

- `/` main voice UI
- `/inbox` legacy stash review UI (see "Stash (retired as a notes store; kept for non-note payloads)" below)
- `/reader` document reader

## Validation Workflow

Before or after backend changes:

```bash
python -m unittest discover -s tests
```

Before restarting after any routing or endpoint change:

```bash
scripts/octavius-models check     # nonzero exit if an alias or token is broken
```

After changes to request routing, WebSocket behavior, reader flows, or inbox flows:

1. Start the app.
2. Open `/`, `/inbox`, and `/reader`.
3. Confirm the WebSocket connects from the browser.
4. Confirm inbox list/load/update still works.
5. Confirm reader document listing and ingest path still work.
6. Check `/health` and confirm `alive`, `ready`, and `degraded` match expectations.
7. Confirm the `llm_chain` section matches the expected endpoint order and current failover state.
8. If startup is degraded, inspect the `mcp.servers` section to see which MCP backends failed to connect.

When touching external-service boundaries, verify the configured endpoints are reachable before assuming an application bug.

### Inspecting a llama.cpp router

`/v1/models` on the llama.cpp routers (lilripper `:8010`, triplestuffed, lilbuddy) returns far more than model ids —
each entry carries the model's **full launch argv** under `status.args`, its
`status.value` (`loaded`/`unloaded`), and `architecture.input_modalities`. So
`--parallel`, `--ctx-size`, and image support are all readable without probing,
and without asking Dave. Do not list only `data[].id` and conclude that is all
there is (this cost a wrong answer on 2026-08-13):

```bash
curl -s -H "Authorization: Bearer $OCTAVIUS_LR_API_KEY" http://lilripper:8010/v1/models \
| python3 -c "
import json,sys
for m in json.load(sys.stdin)['data']:
    a = (m.get('status') or {}).get('args') or []
    g = lambda f: a[a.index(f)+1] if f in a else '?'
    mods = ','.join((m.get('architecture') or {}).get('input_modalities') or ['?'])
    print(f\"{m['id']:34s} {(m.get('status') or {}).get('value','?'):9s} par={g('--parallel'):>2s} ctx={g('--ctx-size'):>7s} in={mods}\")"
```

vLLM endpoints (today, `lilripper:8020`) return a different schema — no `status.args`, `max_model_len` instead — so the recipe only works on llama.cpp routers.

What it still cannot tell you: whether a listed model actually *generates*.
`triplestuffed:8010` (2026-08-08) and the stale `qwen3.5-9b` both listed fine and
hung on completion. For that, send a real completion.

## Architecture

High-level path:

```text
Browser (WebSocket) -> FastAPI app -> main agent (streaming, ~20 core tools)
                                        │
                                        ├─ direct: web search, web page read, vault (notes), reader, PDF, download, memory
                                        │
                                        └─ consult_specialist(domain, task) → subagent
                                             (non-streaming, scoped tools, runs INLINE)
                                             ├─ email domain (evangeline-email)
                                             ├─ research domain (openalex)
                                             └─ tasks domain (vikunja-tasks)
```

The main agent never sees email/research/task tool schemas. It calls
`consult_specialist(domain, task)` which runs a separate non-streaming LLM
loop with only the tools for that domain, using the same MCP sessions. This
keeps the main agent's context lean (~20 tools instead of ~55) and prevents
tool-schema-heavy payloads from causing LLM 500 errors.

`consult_specialist` is **synchronous/inline**: the specialist runs to
completion inside the main agent's tool round (via
`WebSocketSessionHandler.run_inline_subagent`) and its result is returned into
the same turn, so Octavius speaks the answer immediately — no badge, no pull.
A `subagent_dispatcher` ticket is reserved per call so inline consults respect
subagent endpoint capacity, and per-step progress is forwarded to the UI
`status` line so the user isn't left in silence.

**Async delegation is reserved, not removed.** The async path
(`delegate_task` / `pull_delegation` / `list_pending_delegations` /
`cancel_delegation`, the "Agents at Work" badge, the
`spawn_delegation`/`_run_and_announce` lifecycle, the `proactive_speak` setting,
and the `delegation_*` WebSocket messages) is kept in the codebase but is
currently **unexposed** to the agent (no tool specs/handlers registered). It is
reserved for a future `deep_research` domain that will shell out to the **pi
harness headless** (`pi --mode json -p --no-session ...`, parsing the final
assistant `message_end`), which already has a parallel `deep_research`
orchestrator. Re-enabling it is one tool spec + one registry line. Quick
domains (email/tasks/research) deliberately stay inline (low voice latency,
warm MCP sessions); only long-running deep research is backgrounded.

The email subagent prompt uses evangeline's `email_hybrid_search` (RRF fusion of
semantic + BM25) for anything matching on body text. **Folder scope is a closed
set of five values** — `"Inbox"` (the default), `"Read Later"`, `"Follow-Up"`,
`"Todo"`, or All (`folder=null`) — and All requires Dave to ask for it
explicitly ("search all my email", "look in every folder"). Changed 2026-08-14:
the prompt previously told the model to pass `folder=null` on everything, which
searched all 107 folders / 28k messages for questions Dave meant about his
Inbox. Age is deliberately not scope: "it's an old one" means pass
`date_after="1970-01-01"` and *keep* `folder="Inbox"`.

Two traps the prompt now names, both of which cost real turns:

- **Folder names are matched exactly and case-sensitively, and a wrong name
  returns zero results rather than an error** — indistinguishable from "no such
  mail". The prompt used to say `folder="INBOX"`, which matches **nothing**
  (the folder is `Inbox`). A zero-result folder search should be read as a
  suspected typo first.
- The tools disagree on their own defaults (`email_hybrid_search` defaults to all
  folders; `email_keyword_search` / `email_semantic_search` default to Inbox *and* a 6-month
  lookback), so the prompt requires `folder` to be passed explicitly on every
  call rather than relying on any tool's default.

External services currently expected:

- **STT**: faster-whisper at `lilripper:8552/api/transcribe` (large-v3, int8_float16, CUDA)
- **LLM chain (main agent)**: via `OCTAVIUS_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8010/v1/chat/completions` running `qwen3.6-35b-a3b-mtp-general` — a llama.cpp **router**, so the model id selects the model (see "Router model ids" below). `--parallel 4` (re-verified 2026-09-10; was 5 as of 08-26). Accepts image input. **Behind auth**, which means a missing/stale key now 401s the *primary* rather than a fallback — check `/health`'s `endpoints_rejecting_credentials` first when the chain looks flaky. **Promoted from second hop on 2026-08-18** (see the `:8020` bullet for why); it still names the same alias as the subagent and vision *fallbacks*, the reader, and the summary primary, so a turn → consult → document read keeps one model resident with no router swap (the consult and image *primaries* now sit on triplestuffed — see below).
  - first fallback: `lilripper:8020/v1/chat/completions` (**`qwen3.8-27b`**) — repointed 2026-08-26: the `qwen3.6-35b-a3b-mtp-general` alias left this port's catalog, so the entry named a model that no longer existed and every failover into it hard-400'd. This port also **went behind the same auth as `:8010`** (it is no longer open), which is why `OCTAVIUS_LR_API_KEY` now covers both. **As of 2026-09-10 it is a vLLM server, not a llama.cpp router** — it serves `Qwen3.8-27B-AWQ-INT4` (a 4-bit quant, 262k ctx), is effectively always warm (0.3 s, measured 2026-09-10; the old ~26 s cold load is gone), and has no `status.args` in `/v1/models`, so the inspection recipe below does not apply to it. **Demoted from primary on 2026-08-18**: `:8020` is the port Dave loads other models onto by hand, and as the primary, every Octavius turn evicted whatever was there. As a fallback it only answers when `:8010` is genuinely broken. (Under the old llama.cpp setup it was also shared with pi-agent, whose 27B contention produced a live `500 model ... failed to load`; the vLLM replacement changes that failure mode.)
  - second fallback: `triplestuffed:8010/v1/chat/completions` (`gemma4-26b-a4b`) — the only hop on another host, so the only thing keeping this chain alive if lilripper is down. **Re-added after the 2026-08-08 removal** (the Positron-era zombie that accepted connections without generating is gone): it is now a working llama.cpp router — `gemma4-26b-a4b` loaded (`--parallel 4`, 262k ctx, image input) plus `zeta-2.1` — and open (no auth). Verified 2026-09-10.
    - **Why not lilbuddy for this hop: the OOM-cascade rule.** An OOM on lilbuddy takes the *live* voice path down with it, not just a fallback: `:8020` (bge-m3 embeddings) and `:8880` (Kokoro, the only TTS) are separate processes on the same box and both sit on every turn. On 2026-08-18, asking lilbuddy to load `qwen3.6-35b-a3b-mtp-q4-general` produced **502 on every alias and every port on the box** for ~2 min; root-caused by Dave 2026-08-21 — the router ran `max_models=3` with over-generous `--ctx-size` (now 2), so a dense 35B on top of two incumbents exhausted the 128 GB of unified memory. Size any future lilbuddy hop against the whole box's residency budget, not the model alone.
    - **gemma4 is a thinking model that returns reasoning in a separate `reasoning_content` field**, not inline <think> tags — the <think> stripper never sees it and `agent.py`'s `delta.get("content", "")` drops it for free. Measured 2026-09-10 on this host, warm: ~0.2-0.3 s on a trivial prompt, ~4.6-5.7 s on a paragraph-length question (~3500-4300 reasoning chars); the ~28 s figure in `docs/status.md` was lilbuddy end-to-end during the 08-26 auth incident, not this hop. **The trap is still `max_tokens`:** a budget spent on reasoning returns `finish_reason: length` with **empty** `content` — anything that starts sending `max_tokens` here must budget for reasoning tokens.
  - `triplestuffed:8010` was removed from this chain on 2026-08-08 (the Positron zombie) and **re-added as the second fallback** — see the bullet above. The removal was real at the time: it listed fine and never generated, so a failover into it burned the full 120 s read timeout.
- **Subagent LLM chain**: separate routing for delegated subagents via `OCTAVIUS_SUBAGENT_LLM_CHAIN`, defaulting to:
  - primary: `triplestuffed:8010/v1/chat/completions` running `gemma4-26b-a4b`, `capacity: 3` — moved off `lilripper:8010`, which had dropped to `--parallel 4`: a capacity-4 subagent primary would have consumed the whole endpoint, leaving no slot for the main turn, reader, and summaries sharing it. Capacity 3 of triplestuffed's `--parallel 4` deliberately leaves one slot for the vision primary, the main chain's hop 3, and the reader fallback — all the same model on the same host. Chosen for speed and simplicity: `consult_specialist` runs *inline* against the user's turn (the dominant first-turn cost, ~15 s average / 50 s worst on the 35B), and gemma4 answers a real question in ~5 s warm, thinking on. What goes through it is only the three specialist domains — email, research, tasks. `web_search` / `read_url` are direct tools of the main agent and never touch this chain. The old primary — `lilripper:8010` `qwen3.6-35b-a3b-mtp-general`, capacity 4, thinking on — is fallback 1 below.
  - fallback: `lilripper:8010/v1/chat/completions` running `qwen3.6-35b-a3b-mtp-general`, `capacity: 4` — HTTP-level failover, active only when the gemma4 primary is down. Capacity 4 equals `:8010`'s full `--parallel 4`: fine in practice because while a consult runs inline the main turn is blocked awaiting it, not holding a slot. **The dispatcher keeps only the first `fallback` entry** (`subagent_dispatcher.py` — `elif ep.role == "fallback" and fallback is None`), so a second one is dead config; a `:8020` entry was removed for that reason on 2026-09-10.
  - The dispatcher (`subagent_dispatcher.py`) routes by `role`. Only two roles matter per call: `primary` (first-try / concurrency routing, with `secondary` as an optional concurrency-overflow tier) and `fallback` (the single per-call HTTP-failover target passed alongside the assigned URL). Per-endpoint `capacity` controls how many concurrent subagents may share an endpoint.
  - **Model is per endpoint, not per domain.** `subagent.py::_model_for_url` resolves the model from the chain entry matching the assigned URL, so all three specialist domains share one model per endpoint (today, gemma4 on triplestuffed). Per-domain models would need a `model` key on `SUBAGENT_DOMAINS` overriding that lookup.
  - **Router model ids (the alias is load-bearing).** `complete_with_tools` uses each chain *entry's* model (the payload model is ignored) and fails over on any 4xx/5xx, so an alias absent from that endpoint's catalog hard-400s and silently burns a failover hop. **The catalogs churn — Dave rebuilds them. Re-curl `/v1/models` before trusting anything below, and treat a config referencing a missing alias as the first suspect after any router work.** The bare `qwen3.6-35b-a3b` alias exists on **no host in the fleet**. Catalogs re-curled 2026-09-10:
    - `:8010` (9 aliases, same set as 08-26): `qwen3.6-35b-a3b-mtp-{code,general}`, `gemma4-26b-a4b`, `gemma4-31b`, `ministral-14b`, `muse-glimmer-30b`, `qwen3.5-9b`, `qwen3.8-27b`, `qwen3.8-27b-non-thinking`. `qwen3.6-35b-a3b-mtp-general` is resident, `--parallel 4` (down from 5), `--ctx-size 614400`. `qwen3.5-9b` is the known zombie (lists, never generates) — do not point anything at it.
    - `:8020` (**vLLM, not a llama.cpp router**): `qwen3.8-27b` = `Qwen3.8-27B-AWQ-INT4`, `max_model_len` 262144, behind auth. No `status.args` in its `/v1/models`.
    - `triplestuffed:8010` (llama.cpp router, open): `gemma4-26b-a4b` (loaded, `--parallel 4`, 262k ctx, image) and `zeta-2.1` (text-only). Carries the subagent primary, the vision primary, the main chain's hop 3, and the reader fallback.
    - `lilbuddy:8010` (llama.cpp router, open, 8 aliases): `DEFAULT` (text), `gemma4-26b-a4b`, `ling-3.0-flash` (loaded; text-only, 262k ctx, `--parallel 1`), `muse-glimmer-30b`, `qwen3.5-4b`, `qwen3.5-9b`, `qwen3.6-35b-a3b-mtp-q4-{code,general}` (loaded). Image input on everything except `DEFAULT` and `ling-3.0-flash`. The q4 MTP aliases live here. **`ling-3.0-flash` is deliberately off the routing**: it is the designated model for the future `deep_research` domain when the async path is revived — 262k ctx for long tool-heavy loops, `--parallel 1` (one deep-research job at a time is exactly right), async so no live-path latency; keeping it off the voice path also keeps a lilbuddy hop out of the OOM-cascade risk (main chain's second-fallback bullet).
    - **Modalities churn too.** As of 2026-09-10 most lilripper aliases report `text,image`, but `ministral-14b` and `qwen3.5-9b` are text-only, and so are triplestuffed's `zeta-2.1` and lilbuddy's `DEFAULT` / `ling-3.0-flash`. Read `architecture.input_modalities` before assuming a hop can take images, in either direction.
  - **Keep `:8010`'s consumers on one alias.** As of 2026-09-10 that is still nearly everything on that endpoint — main chain primary, subagent fallback 1, vision fallback 1, reader, summary primary — and they all name `qwen3.6-35b-a3b-mtp-general`. If any one of them disagrees, interleaving a document read with a consult thrashes the router between resident models. Same discipline on `triplestuffed:8010`, whose four gemma4 consumers (subagent primary, vision primary, main hop 3, reader fallback) must keep naming `gemma4-26b-a4b`.
  - **Cross-host failover exists now:** the subagent primary sits on triplestuffed, so consults survive a lilripper outage (they fail over to `:8010`, which is up by definition in that scenario). The remaining hole is the two-host case: lilripper *and* triplestuffed both down leaves consults — and the main chain's hop 3 and the vision primary, which share triplestuffed — with nowhere to go. `lilbuddy:8010` is the candidate third host, but see the OOM-cascade rule before putting anything of its on the live path.
- **TTS**: Kokoro at `lilbuddy:8880/v1/audio/speech` (voice `bm_lewis`) — the live
  default. `TTSSettings.voxtral_enabled` is **False**, so every synth call goes
  straight to Kokoro (Voxtral-only voices remap to the fallback voice). Voxtral 4B
  (`OCTAVIUS_TTS_URL`) is wired but disabled — its inconsistent output levels make
  it unsuitable as the live primary. Set `OCTAVIUS_TTS_VOXTRAL_ENABLED=1` to restore
  the Voxtral-primary → Kokoro-fallback path (with circuit breaker).
- **Reader LLM**: `qwen3.6-35b-a3b-mtp-general` at `lilripper:8010/v1/chat/completions` (**behind auth** — needs a bearer token; see "Configuration and secrets"), with `gemma4-26b-a4b` at `triplestuffed:8010` as fallback (added 2026-09-10). The reader is single-endpoint *per call* but the role's second `models.json` entry is its failover target: `reader_text.py::_llm_convert_math` tries each endpoint in turn before degrading to local `strip_latex`, which is what a dead `:8010` used to do to every math chunk (dollar-stripping, silently). `ReaderSettings.llm_fallback_url/model` read the second entry via `_role_single(..., index=1)`; a half-configured entry raises. Note that `LLMChainClient.complete()` merges per-url `params` from the **main** chain, and the fallback URL *is* main-chain hop 3 — so any `params` set on that main-chain entry silently apply to reader fallback calls too (none today). `qwen3.5-9b` is still in `:8010`'s catalog but went stale — it lists in `/v1/models` and then hangs on completion; do not point anything at it. The primary alias deliberately matches the subagent's `:8010` fallback entry.
- **Summary/tag generation**: `lilripper:8010` with fallback `lilripper:8020`, model `qwen3.6-35b-a3b-mtp-general` (moved off the then-dead lilbuddy/triplestuffed pair 2026-08-08 — triplestuffed is back, see the main chain bullet; reordered `:8010`-first 2026-08-18). The order matters more than the traffic volume suggests: summaries fire at conversation *end*, long after Dave has moved on, so with `:8020` primary a background job could quietly evict a model he had loaded there by hand. `SummaryClient` is **not** an `LLMChainClient`, but as of 2026-08-26 it carries a model **per URL** (`summary_model` / `summary_fallback_model`, the latter defaulting to `qwen3.8-27b`). It previously sent one model to both URLs, which became a guaranteed 400 on the fallback the moment the two routers stopped sharing an alias. It does attach `auth_headers` (added 2026-08-08 — previously it sent none, so any authed endpoint here would have 401'd silently). A failed summary is invisible to the user; history just ends up unsummarised and untagged.
- **Embeddings**: bge-m3 chain via `OCTAVIUS_EMBEDDING_CHAIN`, defaulting to:
  - primary: `lilbuddy:8020/v1/embeddings` (standalone llama.cpp bge-m3 server → Caddy :8020 → 127.0.0.1:8002, OpenAI schema)
  - fallback: `workhorse:11434/api/embeddings` (Ollama schema)
  - **Order reversed 2026-08-10, restored 2026-08-12.** lilbuddy was demoted for being
    unreachable (a tailscale fault, since fixed): because a dead host drops packets
    rather than refusing them, every embed burned the full connect budget on it first.
    That is no longer a reason to keep it second — `EmbeddingClient` now has a
    **per-endpoint circuit breaker** (2 consecutive failures → skipped for 300 s, then a
    single half-open probe) and no longer retries `ConnectTimeout` (it subclasses the
    generic timeout class, so a dead host was being retried, doubling the cost). With a
    dead primary capped at two failures and then 300 s of nothing, **order is now purely
    a speed choice**, and lilbuddy measures ~3x faster on a realistic ~1800-char payload
    (0.11 s vs 0.30 s; workhorse also pays ~2.7 s on a cold start after idling).
    `/health`'s `embedding_chain` shows per-endpoint `tripped` / `consecutive_failures` /
    `cooldown_remaining`. It deliberately does **not** feed the top-level `degraded`:
    every search path falls back to keyword matching, so Octavius still answers.
  - **The app no longer embeds at all (2026-09-15).** Message, summary and
    saved-item embedding all moved to the `hybrid-corpus` library.
    `history-index.timer` runs `hybrid-corpus run history` every 15 minutes,
    fingerprints `messages` / `conversations` / `saved_items`, and writes the
    library's sidecars into the same database file. Gone with it: the inline
    `store_embedding` write in `add_message`, the detached `spawn_embedding`
    root task in `add_message_async` (and its 8-in-flight cap), the summary
    embed in `end`/`end_async`, and `history_sweeper`'s repair loop. Recording
    a turn is pure SQLite now, so there is no embedder round-trip anywhere near
    the voice path to detach in the first place. `history_sweeper.run_sweeper`
    / `sweep_once` and `history_enrichment`'s `store_embedding*` /
    `spawn_embedding` are kept as stubs that **raise `RuntimeError`** — a
    silent no-op would let a stale caller look healthy while nothing indexed.
    `drain_inflight()` still runs at shutdown and drains an empty set.
    Consequence: semantic search is **eventually consistent** with a 15-minute
    worst case, except for `saved_items`, which `history_store.save_item`
    indexes inline through the library's `Indexer` (an embed failure there is
    logged and swallowed — the row is committed and keyword-searchable
    immediately, and only the vector is owed).
  - **Embed input cap.** `EMBED_MAX_CHARS` (4000) in `history_enrichment` is now
    only relevant to `embed_text`/`embed_text_async`, which survive as plain
    client wrappers. The library enforces its own `embed_cap` (8000 in
    sites.toml) as a **refusal, not a trim**, and chunks long sources instead
    of truncating them — which is the real fix for the old behaviour where a
    20k-char message was silently indexed by its first 4000 characters.
  - **`conversations.indexed` is gone from the schema** (added 2026-08-10 for
    the sweeper, dropped 2026-09-15). The summariser's `index` flag is no
    longer persisted — it only decides the push to the memory service — because
    the library indexes every conversation with a non-empty summary and nothing
    in the fleet read the column. The live database keeps the column
    harmlessly. `_write_summary` also no longer deletes a stale vector: the
    library re-fingerprints `(service, summary)` on every sync and re-embeds a
    rewrite by itself. The `OCTAVIUS_EMBEDDING_SWEEPER` switch went the same
    day; the retired sweeper never read it.
- **Vision LLM chain**: image-input turns (Matrix `image_input` frames) via `OCTAVIUS_VISION_LLM_CHAIN`, defaulting (2026-09-10) to `triplestuffed:8010` (`gemma4-26b-a4b`) as **primary**, with `lilripper:8010` (`qwen3.6-35b-a3b-mtp-general`) and `lilripper:8020` (`qwen3.8-27b`) as fallbacks, all thinking on. The cross-host primary closes the old lilripper-only limitation — if lilripper is down, image turns still work — and keeps image turns off lilripper's four `:8010` slots, which the voice path owns. The chain stays separate from `llm_chain` because the main chain's third hop is chosen for *availability* rather than for modality — separate chains are what stop a future "add another fallback so voice survives a lilripper outage" edit to `llm_chain` from silently widening where an image turn can land. Check `architecture.input_modalities` before trusting any hop with images: gemma4 takes them, but `ling-3.0-flash`, `zeta-2.1`, `ministral-14b`, and `qwen3.5-9b` do not. **Modality is not enough — check the hop's micro-batch too.** A gemma4v image encodes to 70-1120 tokens by resolution and llama.cpp decodes the whole image chunk as one non-causal ubatch, so a hop launched with the default `-ub 512` *aborts* (`GGML_ASSERT ... n_ubatch >= n_tokens`) on any image bigger than a thumbnail, the router reloads it, and the turn comes back empty ("I'm not sure how to respond to that."). Found 2026-09-14 on the first real Android image turn; fixed with `ubatch-size = 1152` in triplestuffed's `~/.config/llama-router/preset.ini` (`[gemma4-26b-a4b]`), which cost ~1.7 GB of the 3090's headroom (2505 → ~820 MiB). `/v1/models` reports `--ubatch-size` in `status.args` — read it before pointing image turns at any llama.cpp hop. Separate `LLMChainClient` instance (`vision_llm_client` in `service_clients.py`); see `agent.py`'s `use_vision` routing in `stream_agent_turn`. Vision routing is sticky per thread (`Conversation.has_images`): after the first image the whole thread stays on the vision chain and image content arrays stay in memory; on thread re-attach they re-hydrate from the spool via the `attachments` table when the file still exists. Persisted history/memory only ever see text placeholders.
- **PDF → markdown conversion**: driven through the `document-processing` MCP server already registered in `DEFAULT_MCP_SERVERS` (mcp-tools' documents wrapper: scp to lilripper, convert at `lilripper:8251/mcp`, download the .md back to local paths). `docproc_client.py` wraps its `convert_pdf_to_md` / `get_conversion_result` tools via `MCPManager.call_tool`; poll pacing via `OCTAVIUS_DOCPROC_POLL_INTERVAL`/`_TIMEOUT`. Triggered by Matrix `file_input` frames with `mime=application/pdf`; see `docs/ws-media-contract.md`.

Configured MCP servers:

**Tool naming (2026-09-23).** `MCPManager` keys every tool by its model-facing name and routes it to
(server, upstream name). A server with `tool_prefix` in `DEFAULT_MCP_SERVERS` exposes its tools as
`<prefix>_<tool>` (not doubled if the upstream name already starts with it — pi-mcp-adapter's rule).
On a name collision the **first** registered server keeps the bare name and the later one is exposed as
`<server key>_<name>` with a warning — before 2026-09-23 the later server silently overwrote the earlier.
Code that targets one server (e.g. `routes/vault.py`) uses `call_server_tool(server, upstream_name, ...)`
so it never depends on display names. Upstream renames: mcp-tools `RENAMES-2026-09.md`.

- `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`, `tool_prefix: "email"` → `email_hybrid_search`,
  `email_keyword_search` (was `search_emails`), `email_get` / `email_get_many` (were `get_email` / `get_emails`),
  `email_stats`, `email_extract` (was `extract_from_emails`), ... The calendar tools that used to live on this
  server moved to mcp-tools' `server_calendar.py` (:8258); Octavius does not register the calendar server.
- `web-search`: stdio subprocess (mcp-tools' `server_serper.py`, run via its own venv). Exposes a single `web_search` tool — the "search" half of the search → read → reason pipeline. **Serper.dev (Google) is primary; self-hosted SearXNG (`searxng.riegert.xyz`) is a backstop that answers only when Serper *errors*** — an empty Serper result is a valid answer to an obscure query and is not retried against a weaker index. Surfaced directly to the main agent, not behind a specialist. The order was inverted 2026-08-20: SearXNG's `bing` engine was returning topic-unrelated pages while reporting HTTP 200 success (`unresponsive_engines: []`), so roughly two results in three were junk and the old "SearXNG returned nothing" fallback trigger never fired. The backstop is now pinned to `engines=duckduckgo,wikipedia` (`SEARX_ENGINES`). When it answers, the JSON carries `fallback_reason` and `provider: "searxng"`. `server_serper.py` reads `SERPER_API_KEY` from `mcp-tools/.env` and trusts the system CA bundle for SearXNG's Caddy cert on its own (no env needed in the server config). **Without `SERPER_API_KEY` the primary arm is inert — web search runs permanently on the degraded backstop.** Mirrors `pi_harness/extensions/web-search/src/index.ts`; change one, change the other. Replaced the old varlabz `searxng-mcp` (`search` tool, SearXNG-only, no fallback).
- `web-reader`: streamable HTTP at `lilripper:8254/mcp` (mcp-tools' `server_reader.py`, wrapping a self-hosted Crawl4AI `/md` endpoint; same deployed instance the pi agents use). Exposes `read_url` — the "read" half of the search → read → reason pipeline. Surfaced directly to the main agent (like `web-search`), not behind a specialist.
- `vault-search`: streamable HTTP at `triplestuffed:8254/mcp` (mcp-tools' `server_vault.py` — sqlite-vec + FTS5 BM25 over the Obsidian vault, RRF-fused; co-located with the vault). Exposes a single upstream tool `search` (was `search_vault` until 2026-09-23), shown as `vault_search` via `tool_prefix: "vault"`, surfaced directly to the main agent. The `03-personal/Journaling/` subtree is excluded server-side. Search is the only vault operation that goes through MCP — note reads/writes are local file I/O (see "Vault" under Feature Notes).
- `paper-search`: streamable HTTP at `127.0.0.1:8206/mcp` (mcp-tools'
  `server_papers.py` — sqlite-vec + FTS5 BM25 over Dave's converted Paperpile
  library at `/media/extra_stuff/papers/`, RRF-fused; runs as the
  `papers-mcp.service` user unit on this host, no Caddy hop). Exposes
  `search_papers` + `get_paper`, surfaced directly to the main agent.
  Conversion/indexing is owned by mcp-tools (`papers_convert.py` /
  `papers_indexer.py`, nightly `papers-sync.timer`) — see mcp-tools AGENTS.md.
- `openalex`: stdio subprocess via `npm`
- `vikunja-tasks`: streamable HTTP at `triplestuffed:8252/mcp`
- `document-processing`: local stdio wrapper around remote processing on `lilripper:8251/mcp`

## Key Runtime Behavior

- Each WebSocket connection gets its own `Conversation` instance.
- Conversation IDs are persisted in browser `localStorage` and restored with `restore_session`.
- The WebSocket carries both binary audio and JSON messages.
- The agent buffers sentences during tool-call rounds and only emits final spoken text when tool use is complete.
- Tool-call rounds are capped and nudged to stop around rounds 5-6 of 7.
- Tool results are truncated to 4000 characters to protect context budget.
- Qwen `<think>...</think>` output is stripped before user-visible text or TTS.
- Response style is channel-aware. `stream_agent_turn` takes a `source` and
  folds a per-turn style directive into `messages[0]` (same mechanism as the
  memory block): `source="voice"` → short/spoken/no-markdown; every other
  source (`text`/`matrix`/`image`/`file`/`inbox_chat`) → may use light markdown
  and give a complete answer. The base `settings.system_prompt` is
  channel-neutral; the directive (`VOICE_STYLE`/`TEXT_STYLE` in `agent.py`) is
  the tuning knob. Defaults to `"voice"` if a caller omits it.
- Spoken text is markdown-normalized before TTS. `tts.synthesize` runs every
  string through `speechify` (the single choke point all TTS callers share),
  stripping `**bold**`/`*italic*`/`` `code` ``/links/headings and line-leading
  list markers so the engine doesn't verbalize "asterisk asterisk". It also
  strips ORPHAN emphasis left when a bold/italic span is split across streamed
  sentences, while preserving meaningful characters (`3 * 4`, `foo_bar`). It is
  deliberately lighter than `reader_text.clean_for_speech` (which also strips
  citations/LaTeX for converted journal PDFs).
- Conversation history trims automatically to 40 messages.
- `/health` distinguishes `alive`, `ready`, and `degraded` states.
- `/health` exposes per-server MCP connection status plus `llm_chain` observability including configured endpoints, failover count, terminal failures, the last successful endpoint, and **failure classification** — `endpoints_rejecting_credentials` / `auth_failures` / `last_failure_kind`, plus per-endpoint `last_error_kind`, `last_error_status`, and `authenticated`. Check `endpoints_rejecting_credentials` first when a chain looks flaky: non-empty means a key problem, not an outage.

WebSocket message families:

- Voice: `status`, `transcript`, `transcript_partial`, `response`, `reset`, `restore_session`, `session_id`, `load_conversation`, `conversation_loaded`, `stt_start`, `stt_stop`, `stt_auto_stop`
- Text streaming (server→client): `response_delta` — one sentence of the reply as it streams, emitted before the final `response` (which stays authoritative). The Matrix sidecar edits its thread message in place from these; the browser ignores the frame.
- Matrix media (client→server, same session/threading semantics as `text_input`): `image_input`, `file_input` — frozen contract, see `docs/ws-media-contract.md`. Both repos (`octavius`, `matrix-agent-sidecar`) implement against that doc.
- Reader: `reader_play`, `reader_pause`, `reader_stop`, `reader_position`, `reader_audio_done`
- Item chat: `item_chat`, `item_chat_load`, `item_chat_reset`, `item_chat_response`, `item_chat_loaded`, `item_chat_status`
- Delegations (DORMANT — reserved for future `deep_research`, not currently emitted): `delegation_update` (server→client; status running/ready/failed + preview), `delegation_removed` (server→client; record cleared), `delegation_list` (client→server; resync request), `delegation_pull` (client→server; mode=merge|new), `delegation_dismiss` (client→server)

## Code Map

Core runtime:

- `main.py` - FastAPI app creation, startup wiring, shared top-level routes, WebSocket entrypoint
- `db.py` - SQLite connection helpers and short-lived connection context manager
- `settings.py` - env-backed runtime settings and defaults; also loads `models.json` model routing (`_role_chain` / `_role_single`, precedence: defaults < file < env; the reader's second entry is its fallback, read via `_role_single(..., index=1)`)
- `scripts/octavius-models` - show / check / apply model routing; `check` probes every endpoint's catalog and auth before `apply` will restart
- `service_clients.py` - core HTTP clients for STT, TTS, the main LLM chat chain, summary generation, and embeddings
- `media_uploads.py` - streaming/sanitizing/cap logic for `POST /api/media/upload` (client media, e.g. Android), pure functions plus `save_upload`; FastAPI-free and unit-testable on its own
- `stt.py` - thin STT wrapper
- `tts.py` - thin TTS wrapper; `speechify` markdown→speech normalization applied at the `synthesize` choke point
- `vad.py` - Silero VAD ONNX wrapper for server-side voice activity detection

Route modules:

- `routes/inbox.py` - inbox page and inbox REST API routes
- `routes/conversations.py` - conversation history API routes
- `routes/media.py` - `POST /api/media/upload` for non-sidecar clients (Android); thin adapter over `media_uploads.py`
- `routes/reader_api.py` - reader page and reader REST API routes
- `routes/vault.py` - vault REST API (`/api/vault/{recent,note,search}`); search proxies the vault server's `search` tool via `call_server_tool("vault-search", "search", ...)`, everything else is local file I/O via `vault_files.py`

Conversation and tool loop:

- `conversation.py` - chat history state with trim/reset/load support
- `agent.py` - LLM loop, tool calling, output cleanup, tool-spiral prevention
- `websocket_session.py` - WebSocket session state, message dispatch, item-chat lifecycle, and STT/TTS turn handling
- `mcp_manager.py` - MCP client lifecycle, routing, truncation, reconnect handling
- `tools.py` - local tool schemas and dispatch entrypoint used by the agent loop
- `subagent.py` - internal scoped subagent for specialist domains (email, research, tasks); invoked inline via `consult_specialist` (`run_inline_subagent`)
- `local_tool_specs.py` - local tool schemas
- `local_tool_registry.py` - compatibility wrapper for older local-tool imports
- `local_tool_downloads.py` - local download filename logic and download tool execution
- `local_tool_vault.py` - `save_note` / `read_note` / `edit_note` / `commit_edit` tool handlers over `vault_files.py`
- `vault_files.py` - pure file I/O over the Obsidian vault (path-safe, journaling-denied, atomic hash-guarded writes); search never goes through this module
- `local_tool_inbox.py` - legacy stash save/read helpers (`save_to_stash` / `list_stash_items` — retired/unwired, no longer registered as tools)
- `stash_to_obsidian.py` - watermark-based one-way exporter of `saved_items` rows to the vault inbox (run via the `obsidian-stash-export.timer` user unit)
- `local_tool_reader.py` - local reader handoff (file, and raw text via `read_document(text=...)`) and background PDF-processing helpers
- `local_tool_documents.py` - `check_document_status` local tool (polls a docproc job by id)
- `docproc_client.py` - loopback HTTP client for the docproc web queue (submit/poll a PDF conversion job); Octavius never imports the `docproc` package

Reader pipeline:

- `document_sources.py` - file/source sniffing, decoding, PDF detection
- `reader_ingest_service.py` - narrow entrypoints for starting and retrying reader ingest jobs
- `reader_ingest_handlers.py` - source-specific ingest handlers for files, URLs, PDFs, retry scheduling, and conversion polling
- `reader_store.py` - reader document CRUD, speech-file lookup, stale-job cleanup, and the per-document appendix files that let a retry reproduce appended text
- `reader_text.py` - markdown chunking, math-to-speech conversion (per-endpoint failover before the local `strip_latex` degradation), and speech JSON generation; `append_to_document` extends an existing speech file in place
- `reader_playback.py` - sentence-by-sentence playback streaming over WebSocket

History and inbox:

- `history.py` - DB bootstrap, conversation/session recording, and compatibility re-exports for history/inbox helpers
- `history_enrichment.py` - summaries and topic tags; the embed-write helpers are retired stubs that raise (`drain_inflight` still live)
- `history_sweeper.py` - RETIRED 2026-09-15; both entry points raise. Kept so the retirement is visible from the import graph
- `history_store.py` - conversation queries, inbox CRUD, hybrid search over the library's `history_summaries` / `history_saved_items` collections, `assert_history_serveable`, memory-push watermarks
- `schema.sql` - SQLite schema for the app-owned tables only; the library owns and creates its own sidecars

Frontend:

- `static/app-common.js` - shared browser helpers for WebSocket setup, HTML escaping, and voice-list loading
- `static/index-audio.js` - streamed TTS queue, silence trimming, and browser audio playback helper for the main voice UI
- `static/index-app.js` - main voice UI controller, settings/history overlays, transcript rendering, and WebSocket client logic
- `static/inbox-app.js` - inbox page behavior, filtering, expansion, and item-chat client logic
- `static/reader-app.js` - reader page behavior, document list, retry flow, and playback client logic
- `static/index.html` - main voice UI shell
- `static/inbox.html` - inbox review UI with inline item chat
- `static/reader.html` - reader UI with playback controls and polling
- `static/manifest.json` - PWA manifest

Tests:

- `tests/test_main.py`
- `tests/test_conversation.py`
- `tests/test_mcp_manager.py`
- `tests/test_reader.py` - also `_llm_convert_math` endpoint failover (primary → fallback → `strip_latex`)
- `tests/test_reader_ingest_handlers.py`
- `tests/test_reader_ingest_service.py`
- `tests/test_reader_append.py` - appending to an existing document: incremental chunk append, position preservation, `replace`, retry folding appendices, status rules, tool/route wiring
- `tests/test_reader_edit.py` - renaming a document: DB + speech JSON title update, missing speech file, validation, any-status rename, route wiring
- `tests/test_document_sources.py`
- `tests/test_websocket_session.py`
- `tests/test_history_attach.py`
- `tests/test_history_enrichment.py` - also asserts the retired embed path: recording a message embeds nothing, leaves nothing in flight, and the retired helpers raise
- `tests/test_history_sweeper.py` - the sweeper's retirement contract, plus the `last_extracted_message_id` migration tests (which never concerned the sweeper)
- `tests/test_history_store.py` - includes library-backed search tests over a throwaway DB with `FakeEmbedder`: service/source/since filters, result shape, dismissed-item exclusion, and the lexical-only degradation when the embedder is down
- `tests/test_local_tool_handlers.py`
- `tests/test_local_tool_history.py` - search filters/list mode and `read_conversation` paging
- `tests/test_local_tool_reader.py`
- `tests/test_local_tool_registry.py`
- `tests/test_local_tool_inbox.py`
- `tests/test_local_tool_vault.py`
- `tests/test_routes_vault.py`
- `tests/test_vault_files.py`
- `tests/test_media_upload.py` - `media_uploads.save_upload` (sanitization, mime resolution, streaming caps) and the `/api/media/upload` route
- `tests/test_subagent.py`
- `tests/test_subagent_dispatcher.py`
- `tests/test_agent.py` - vision-chain routing and image-turn history downgrade in `stream_agent_turn`
- `tests/test_service_clients.py` - LLM chain failover/health, TTS circuit breaker, embedding schemas, LLM endpoint auth headers, and per-entry `params` merging
- `tests/test_settings_models_file.py` - `models.json` precedence, validation, `params` passthrough, and the reader fallback entry
- `tests/test_tts.py` - `speechify` markdown→speech normalization
- `tests/test_docproc_client.py`
- `tests/test_local_tool_documents.py`

## Feature Notes

### Voice Interaction

Three talk modes, selectable in the settings panel:

- **Hold to talk** (default): press and hold to record, release to send.
- **Tap to talk**: tap to start recording, tap again to stop.
- **Continuous conversation**: press "Start Conversation" to begin a hands-free
  loop. The user speaks, Silero VAD detects 1.5s of silence and auto-stops,
  Octavius responds via TTS, then the mic automatically reopens for the next
  turn. Press "End Conversation" to exit the loop.

Streaming STT: the browser captures PCM at 16kHz via Web Audio API
ScriptProcessor and sends binary chunks every 250ms over the WebSocket. The
server accumulates a buffer, runs background faster-whisper transcription, and
sends `transcript_partial` messages. On stop (manual or VAD auto-stop), the
server uses the latest partial text immediately — no re-transcription needed.

Server-side VAD: Silero VAD v6 ONNX model (`models/silero_vad.onnx`) runs on
CPU via onnxruntime. Each 512-sample window (32ms at 16kHz) is prepended with
a 64-sample context buffer before inference. Per-session LSTM state is carried
between chunks and reset at the start of each turn. The `SileroVAD` class in
`vad.py` wraps the ONNX model; each WebSocket session gets its own instance.

### Vault (Obsidian notes — the single note store)

As of 2026-07-09, Dave's Obsidian vault is the single source of truth for
notes; the DB stash write path is retired. The vault (`VAULT_PATH`, default
`~/Documents/Personal`) is plain `.md` files on triplestuffed.

- Agent tools (local, in `local_tool_vault.py` over `vault_files.py`):
  `save_note`, `read_note`, `edit_note`, `commit_edit`. Search is the
  `vault_search` MCP tool (vault-search server, upstream `search`), which reads a derived
  sqlite-vec + FTS5 index, never the files directly.
- Frozen vault API contract rules, enforced in `vault_files.py`: new notes
  land in `00-zettelkasten/001-Fleeting/` only (filename frozen at creation);
  `03-personal/Journaling/` is never listed, read, or written; paths are
  vault-relative POSIX with traversal/symlink escapes rejected; writes are
  atomic (temp file + `os.replace`, umask-honoring 0664) and hash-guarded
  (`base_hash` = sha256 of file bytes, optimistic concurrency — `commit_edit`
  409s on conflict).
- REST surface for UI/clients (e.g. Android): `GET /api/vault/recent`,
  `GET/POST/PUT /api/vault/note`, `GET /api/vault/search` (`routes/vault.py`).
- Agents never rename or move notes — Dave files them in Obsidian himself.

### Stash (retired as a notes store; kept for non-note payloads)

The old DB capture area (`saved_items` in `octavius_history.db`). The
*notes* write path is retired: `save_to_stash` / `list_stash_items` still
exist in `local_tool_inbox.py` but are unwired (no tool specs/handlers
registered), and `stash_to_obsidian.py` exported existing items to the vault
one-way (watermark-based, via the `obsidian-stash-export.timer` user unit).
This supersedes the old "Stash rename" TODO (routes `/inbox` → `/stash`).

The stash is NOT being deleted, though: Dave wants it kept for payloads that
don't belong in Obsidian — first planned use is a transcription/dictation
mode that saves raw transcripts to `saved_items` (see `docs/status.md`
Near-Term Work #5; "there are use-cases for the Stash database still — just
not typical notes").

Still live:

- `process_pdf` background conversions write their result to a stash item.
- The `/inbox` review UI and `/api/inbox/*` routes still work
  (list/update/`DELETE /api/inbox/{id}`, bge-m3 semantic search).
- Item chat still works, but the item's (capped) content is now inlined into
  the prompt — the `read_item_content` tool was removed.

### Document Reader

- Accepts local files, URLs, inbox items, and **raw pasted text**.
- Converts PDF, markdown, and extracted HTML content into speech-oriented JSON.
- HTML extraction uses trafilatura.
- Math-heavy paragraphs are sent to the reader LLM; non-math paragraphs are cleaned locally.
- Playback is streamed sentence-by-sentence over WebSocket with position sync.
- Document list auto-polls while any document is still `processing`.
- Failed reader documents can be retried from the stored source metadata via `POST /api/reader/documents/{id}/retry`.

Reader storage:

- speech-ready JSON files: `/home/dave/octavius-reader/`
- pasted-text originals: `/home/dave/octavius-reader/pasted/<doc_id>-<slug>.md`
- metadata: `reader_documents` table

Appending to an existing document (`append_to_reader_document` tool,
`POST /api/reader/documents/{id}/append {"text"|"path", "replace"?}`) is for
when the original pull came back partial — a sign-in pop-up, a paywall
teaser — and Dave supplies the rest by hand. `reader_text.append_to_document`
is **incremental**: only the new text is chunked and math-converted, and the
existing chunks keep their bytes and indices so `last_chunk`/`last_sentence`
still point where Dave left off (re-chunking the whole document would not
guarantee that: a section that grows past three paragraphs is re-split).
`replace=true` rebuilds from the new text alone, for when what the reader
holds is the sign-in page. A `failed` document may be appended to (that is
the sign-in-wall fix); a `processing` one 409s, and the status flip is a
conditional UPDATE (`reader_store.claim_document_for_processing`) so two
concurrent appends cannot both launch a job.

Renaming a document (`PATCH /api/reader/documents/{id} {"title": "..."}`, via
`reader_ingest_service.rename_reader_document` → `reader_text.rename_document`)
is allowed in any status, including `processing`. The title is stripped,
rejected if blank or over 200 characters, and written to the DB row; if a
speech JSON exists its stored title is rewritten too (temp + `os.replace`,
the same atomic-write helper `_write_speech` uses), but a missing or
unreadable speech file is not an error — the rename still succeeds. Nothing
reads the JSON title (playback and both clients use the row), so it is kept
consistent cheaply rather than transactionally: while the document is
`processing` the rename leaves the JSON to the running job, and `_write_speech`
re-reads the row's title just before it writes, so a rename that lands mid-job
is neither clobbered by the job's final write nor able to clobber the job's
freshly converted chunks. Both the /reader page and the Android client expose
append / replace / rename directly (an edit panel on each ready or failed card
and in the player), polling the document until it settles; a failed append on
a previously-ready document comes back `ready` with `error` set, which the
clients must treat as a failure, not a no-op.

The append is **atomic from the document's point of view**: the appendix file
is written only after conversion succeeds and just before the speech file is
replaced (temp file + `os.replace`), so a failed append — reader LLM down,
disk full — leaves a `ready` document `ready` with the failure in `error`, and
no orphaned appendix on disk. Each appended block lives at
`<reader_dir>/appendices/<id>/<seq>-<stamp>.md`; `ingest_document` folds them
onto the end of the base text, which is what keeps a **retry** (which replays
the original source) from silently dropping appended text — so persistence is
mandatory, not best-effort. A `replace` leaves a `REPLACED` marker in that
directory: `with_appendices` then drops the base text, and `start_retry_task`
skips the source replay entirely (for a URL, replaying it would fail on the
sign-in wall again before the real text was ever folded in). A `path` is read
the way file ingest reads one (HTML through trafilatura); PDFs are refused —
read them as their own document. `replace` is parsed strictly (`"false"` is
false), because `bool("false")` would have destructively rebuilt the document.

Pasted text (`source: "text"`) is the one source with no file or URL behind it,
so `start_text_ingest` writes it out and records the path as `source_path`.
That is what makes it retryable: `start_retry_task`'s existing `markdown`
branch re-reads `source_path`, so retry needed no new code. Writing it out is
best-effort — a failure costs retryability, never the document. Both entry
points (the `/reader` paste box and the agent's `read_document(text=...)`) go
through `start_text_ingest`, so both get title derivation and persistence.

### Media turns (image / PDF) — Matrix and the Android client

The Matrix sidecar (`../matrix-agent-sidecar`) spools attachments to
`/media/extra_stuff/octavius/matrix_media/` and sends `image_input` /
`file_input` WS frames — see `docs/ws-media-contract.md` for the frozen
wire contract both repos implement against.

**Client uploads (2026-09-13)**: a client that isn't the sidecar — today, the
Android app — has no spool of its own, so it POSTs the file to Octavius first:
`POST /api/media/upload` (`routes/media.py` / `media_uploads.py`), multipart
with one `file` part, streamed in chunks (never buffered whole) into
`settings.media_upload_dir` (`OCTAVIUS_MEDIA_UPLOAD_DIR`, default
`/media/extra_stuff/octavius/client_media/`) under a sanitized
`<12-hex>-<name>` filename. The 20 MB/50 MB caps are logical, not an ingress
limit — Starlette's parser has already received the whole body into its own
spooled temp file before `save_upload` sees it — so a `Content-Length`-based
413 before parsing is the only pre-receipt bound (a real guard needs a Caddy
body-size limit on `octavius.riegert.xyz`, sudo, not done). The response
(`path`/`mime`/`filename`/`size_bytes`) is what the client then sends
verbatim in the existing `image_input`/`file_input` WS frame below, but
those handlers no longer trust it outright: `path` is resolved against the
`OCTAVIUS_MEDIA_SPOOL_DIRS` allowlist (`media_uploads.resolve_spooled_media`,
symlinks resolved before the containment check) and `handle_image_input`
re-stats the file against `IMAGE_MAX_BYTES` rather than trusting
`size_bytes`; every rejection sends `status: audio_done` too, so a rejected
frame can't leave a client's turn hanging. Like the Matrix spool, this
directory is **not garbage-collected yet** — a follow-up.

- **Images** (`image_input`): `websocket_session.handle_image_input`
  base64-reads the spool file and builds an OpenAI-style multimodal content
  array. The turn routes through `settings.vision_llm_chain` instead of the
  default chain (see `agent.py`'s `use_vision` handling in
  `stream_agent_turn`). The image content array **stays** in the in-memory
  conversation and the thread stays on the vision chain for its remaining
  turns (`Conversation.has_images` is sticky until reset/load), so follow-up
  questions about the same image still see the pixels rather than a summary.
  llama.cpp's prefix cache absorbs the re-sent image tokens; payload growth is
  bounded by `Conversation.trim()`. `Conversation.replace_last_user_content`
  implements a downgrade-to-placeholder but is **not called from production
  code** (tests only) — an earlier design that was dropped in favour of
  keeping the image. Persisted history and the memory extractor only ever see
  the placeholder/caption text, never base64.
- **PDFs** (`file_input`, `mime=application/pdf`): submitted to the docproc
  web queue deterministically (plain code, not an LLM tool call) via
  `docproc_client.py`. A non-empty caption is treated as instructions —
  Octavius polls the job in a background task (bounded, doesn't block the WS
  loop or other sessions) and then runs an agent turn with the instructions
  plus the converted markdown (inlined under
  `OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET`, else path + head excerpt). An empty
  caption gets an immediate acknowledgement mentioning the docproc job id.
  The `check_document_status` local tool lets the model check status / fetch
  the markdown path for a job id later in the conversation. Converted outputs
  normally land next to the source PDF; when the source dir is read-only for
  the service user (the Matrix spool), the mcp-tools wrapper falls back to
  `~/docproc-output/<stem>-<path-hash>/` on this host. Job ids are in-process
  wrapper state and do NOT survive an Octavius restart —
  `check_document_status` on an old id reports it unknown.
- **Non-PDF files** (`file_input`, other `mime`): a brief acknowledgement
  turn only — Octavius can't process other file types yet. Matrix audio
  (voice messages) and video never reach Octavius at all: the sidecar
  degrades them to descriptive `text_input` frames.
- Both flows record an `attachments` row (`type` `image`/`file`) against the
  persisted user message.

### Conversation History

- Conversations are recorded in `octavius_history.db`. **The live database is not the
  one in the repo**: the service unit sets `OCTAVIUS_DB_PATH=/media/extra_stuff/octavius/octavius_history.db`,
  and the repo-local file is a stale leftover. Query the former when inspecting real
  state — `systemctl --user show octavius -p Environment` is the authority.
- **Indexing is not the app's job (since 2026-09-15).** Octavius writes
  `messages`, `conversations.summary` and `saved_items` and stops there.
  `history-index.timer` runs `hybrid-corpus run history` every 15 minutes,
  fingerprints those three tables, and embeds what changed into the library's
  sidecars (`history_messages_*`, `history_summaries_*`,
  `history_saved_items_*`) in the same database file. The legacy vec0 tables,
  the inline/detached embed writes and `history_sweeper` are all gone; the
  sweeper and `history_enrichment`'s store/spawn helpers survive as stubs that
  raise. `main.py`'s lifespan calls `history_store.assert_history_serveable`,
  which refuses to start if `OCTAVIUS_DB_PATH` and sites.toml's
  `[corpora.history] database` disagree, or if the sidecars are missing or
  not cosine-metric.
- Summaries and topic tags are generated when a conversation ends. The summary
  prompt asks for a one-sentence, action-oriented summary *and* an `index`
  flag. The flag decides only whether the conversation is pushed to the
  memory service; it is not persisted, and it gates no embedding — the
  library indexes every conversation with a non-empty summary. Tags are
  still generated for all conversations.
- The main agent can search prior Octavius conversations via the
  `search_conversation_history` local tool, which wraps
  `history_store.search_conversations()`. That is hybrid search over the
  `history_summaries` collection now: a vec0 KNN arm and an FTS5/BM25 arm
  fused with RRF. An embedding outage degrades to lexical-only rather than
  raising or returning nothing. Filtered to `service="octavius"` and excludes
  the current conversation. Optional `source` (`voice`/`matrix`/`text`) and
  `since` filters are composed onto the adapter's own `service` filter; with a
  filter and no query it becomes a recency listing
  (`history_store.list_conversations`), which also surfaces conversations with
  no summary at all.
- The `read_conversation` local tool returns the full transcript of a prior
  conversation by id (user/assistant turns only), channel-agnostic — this is
  what lets a Matrix thread pull in a past voice conversation and continue it
  in text. Transcripts are paged (page 1 = most recent, ~3.5k chars/page,
  long single messages capped) because local tool results bypass the MCP
  4000-char truncator. Handlers live in `local_tool_history.py`.
- History can be resumed from the browser UI through `load_conversation`.
- The same DB is shared with other AI services and exposed through the conversation-history MCP server.
- Request handlers and background reader jobs use short-lived SQLite connections; live conversation history sessions keep their own dedicated connection until the session ends.

## Contributor Guidance

Prefer these refactor directions:

- keep core STT/TTS/LLM chat boundary code in `service_clients.py` and related wrappers
- keep `main.py` focused on routing and startup, not orchestration
- keep local tool schemas in `local_tool_specs.py`, with dispatch centered in `tools.py`
- keep inbox/history query logic out of route handlers

When adding a feature:

1. Decide whether it belongs in core voice flow, reader flow, inbox/history, or a tool/MCP boundary.
2. Add or update tests near the affected subsystem.
3. Update this file only if the change affects stable architecture, operational workflow, or contributor expectations.

## Extending Octavius

Adding functionality is straightforward now, but most changes still touch a few boundaries at once. The main design question is where the new behavior should live, not how to wire it into a monolith.

Use these placement rules:

- voice/session behavior belongs in `conversation.py`, `agent.py`, or `websocket_session.py`
- new HTTP routes belong in the relevant `routes/*.py` module, with orchestration pushed down into subsystem modules
- reader ingest and playback changes belong in the `reader_ingest_*`, `reader_store.py`, `reader_text.py`, or `reader_playback.py` modules
- inbox/history query and persistence changes belong in `history_store.py` or `history_enrichment.py`, not in route handlers
- local tool additions belong in `local_tool_specs.py` plus the appropriate `local_tool_*` execution module, then get wired through `tools.py`
- new outbound service integrations should go behind `service_clients.py` or a closely related wrapper, not inline in feature code

Common extension patterns:

1. Add a new local tool.
   Update `local_tool_specs.py`, implement the behavior in the right `local_tool_*` module, wire it through `tools.py`, and add tests for both the handler behavior and dispatch path.

2. Add a new reader source or ingest mode.
   Start in `reader_ingest_service.py` for the entrypoint shape, put source-specific logic in `reader_ingest_handlers.py`, keep document metadata in `reader_store.py`, and keep markdown-to-speech logic in `reader_text.py`.

3. Add a new UI action or page.
   Put the route in the relevant router module, keep browser logic in the page-specific `static/*-app.js` file, and extend `static/app-common.js` only for behavior that is genuinely shared.

4. Add a new external dependency or backend call.
   Put timeouts, retries, fallback behavior, and health/observability hooks near the client boundary. If the dependency can fail independently, make sure `/health` or logs surface the degraded state clearly.

Keep these considerations in mind:

- avoid putting business logic back into `main.py`; use it for composition and top-level routes only
- preserve short-lived SQLite connection usage for request/background work; do not reintroduce a shared app-wide connection
- if a feature creates background tasks, decide explicitly what happens on restart and whether retry/requeue is needed
- if a feature depends on MCP or LLM availability, think through degraded behavior and user-visible failure messages
- if a feature changes a persisted shape or workflow, update both docs and tests in the same change
- prefer extending existing subsystem seams over adding another thin facade layer

Minimum completion bar for a new feature:

1. the code is placed in the correct subsystem boundary
2. the happy path works
3. at least one failure or degraded-path test exists where it matters
4. `/health`, logs, or user-visible status remain understandable if the feature depends on outside services
5. `AGENTS.md` is updated if the stable architecture or contributor workflow changed

## Current Hotspots

These are still the main places where complexity is concentrated:

- `main.py` still owns a broad REST and startup surface
- frontend behavior is now split into dedicated JS assets, but the UI still uses large static HTML shells rather than smaller components/templates
- reader responsibilities are split more cleanly now, but ingest flow still spans several modules and background-task boundaries

For current refactor notes, recent fixes, and change-oriented status, see `docs/status.md`.

## Related Docs

- `README.md` - short setup and development commands
- `docs/status.md` - current refactor notes, recent fixes, and active hotspots
- `docs/ws-media-contract.md` - frozen WS media contract (`image_input`/`file_input`) shared with `matrix-agent-sidecar`
- `docs/HANDOFF-matrix-latency.md` - Matrix first-turn latency: measurements, the streaming + subagent-routing changes, and the open 502 chase
- `octavius-prd.md` - broader product/design document
- `octavius-android-design.md` - Android companion app design exploration

## Native Android client

A working native client lives in the sibling repo `../octavius-android` (Kotlin/Compose
foreground-service app; its own `CLAUDE.md`). It is an **independent client of the same
`/ws`** as the browser PWA — each WS connection gets its own `Conversation`, so both run
side by side with no server change. It speaks the exact `static/index-app.js` protocol
(Float32 LE PCM @16k up; `transcript`/`response`/`status`(incl. `audio_done`)/`stt_auto_stop`
+ WAV down).

**Before changing the WS protocol or STT/VAD/audio_done behavior, know the app depends on:**
the server VAD only auto-stops *after* speech (pure silence never auto-stops), and an empty
transcription sends **no `audio_done`** (just a "Couldn't hear anything" status). The app's
continuous-conversation loop and silence watchdog are built around exactly this (its wake
word is shelved as of 2026-07-11; the app only holds a mic during a capture or an active
conversation). If you change it, the PWA *and* the Android client change together.

**Barge-in:** `handle_stt_start` now cancels any in-flight `turn_task` — starting a new
capture means the user is talking, so the current reply (LLM stream + TTS) is stopped. This
is a no-op in normal flows (the turn is already done by the time the mic reopens) and lets a
client interrupt a long reply by opening capture mid-stream. The Android client uses this for
its "interrupt while speaking" feature (energy-gated detector during playback). A future PWA
Stop button could send `stt_start`/cancel the same way.

## Coding-agent access (Claude Code, Codex)

This file is the shared instruction set: `CLAUDE.md` is a symlink to
`AGENTS.md`, so Claude Code and Codex read the same text. MCP access differs
per agent and is configured in two committed files:

- **Claude Code** reads `.mcp.json` (you approve the servers on first launch
  in this repo): `conversation-history` and `paper-search`.
- **Codex** reads `.codex/config.toml`: `paper-search` only. Codex is
  cloud-connected, so it deliberately does not get `conversation-history`.

Configured servers:

- `conversation-history`: `http://127.0.0.1:8203/mcp` — the conversation-history
  MCP server (`mcp-tools/server_history.py`), running as the
  `conversation-history.service` user unit on triplestuffed.
- `paper-search`: `http://127.0.0.1:8206/mcp` — Dave's paper library
  (`mcp-tools/server_papers.py`, `papers-mcp.service`). No PII; safe for
  cloud-connected clients, unlike vault-search.

The conversation-history server includes inbox-related tools such as `save_to_inbox`, `search_inbox`, `list_inbox`, `get_inbox_item`, and `update_inbox_item`.

`vikunja-tasks`, `evangeline-email`, and `vault-search` are intentionally **not** exposed to either coding agent (personal data / PII).
